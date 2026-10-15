"""The daily ``tides.acquire`` job: store the tide predictions of every spot's tide point.

For the window from yesterday 00:00 UTC to TIDE_HORIZON_DAYS + 1 day ahead (the extra day
gives the last forecast hours a following high or low water), per provider in
TIDE_PROVIDERS:

* ``noaa_coops``: every CO-OPS station linked to an active spot. High and low waters
  (``interval=hilo``) and, for a harmonic station, its own curve every 30 minutes
  (``interval=30``), both relative to MLLW. A subordinate station only has high and low
  waters: its curve is the cosine interpolation of :func:`coops.cosine_curve`, stored with
  ``interpolated = true``. Once a week the station's kind and reference station are
  checked against CO-OPS's station list (about 2 MB) and its datums (MSL above MLLW ...)
  are refreshed. Otherwise 1 small request per subordinate station and 2 per harmonic
  station a day (9 requests, 119 KB for the 6 US stations, measured 2026-10-15).
* ``harmonic_model``: every model point (EOT20 cell) linked to an active spot: the curve
  every 30 minutes and the high and low waters (2-minute synthesis, parabolic vertex)
  computed locally from the committed constants, datum ``model_msl``, source ``eot20``.
  No network access.

Rows are upserted on (station, valid_time, datum, is_event), so a re-run changes nothing
but ``fetched_at``; rows of the window that the new predictions no longer contain (an
event that moved by a minute) are deleted, so a station never shows two versions. One
station's failure never stops the others; the job fails only when every station failed.
The catalog (data/tide_stations.json) is re-seeded first, so a new deployment needs no
separate step.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import Boolean, delete, func, literal_column, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.models import SpotTideStation, SurfSpot, TidePrediction, TideStation
from app.services.http import ProviderError, TransientProviderError
from app.services.tides import coops, harmonic_model
from app.services.tides.catalog import seed_tide_catalog

logger = logging.getLogger(__name__)

CURVE_STEP = timedelta(minutes=30)
UPSERT_CHUNK = 2000
# Station kinds and datums change rarely; the station list is about 2 MB.
STATION_CHECK_INTERVAL = timedelta(days=7)
UNIQUE_CONSTRAINT = "uq_tide_predictions_station_id_valid_time_datum_is_event"


def acquisition_window(now: datetime, settings: Settings) -> tuple[datetime, datetime]:
    """[yesterday 00:00 UTC, now + TIDE_HORIZON_DAYS + 1 day rounded up to the hour]."""
    now = now.astimezone(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    end = now + timedelta(days=settings.tide_horizon_days + 1)
    end = end.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return start, end


def _linked_stations(db: Session, provider: str) -> list[TideStation]:
    """The provider's stations linked to at least one active spot."""
    query = (
        select(TideStation)
        .join(SpotTideStation, SpotTideStation.station_id == TideStation.id)
        .join(SurfSpot, SurfSpot.id == SpotTideStation.spot_id)
        .where(TideStation.provider == provider, SurfSpot.is_active)
        .distinct()
        .order_by(TideStation.code)
    )
    return list(db.scalars(query).all())


def store_predictions(
    db: Session,
    station: TideStation,
    points: Iterable[coops.TidePoint],
    *,
    source_code: str,
    start: datetime,
    end: datetime,
    fetched_at: datetime,
) -> dict[str, int]:
    """Upsert one station's predictions for [start, end] and delete the window's rows the
    new set no longer has (same datum). Returns inserted / updated / removed counts."""
    rows = [
        {
            "station_id": station.id,
            "valid_time": p.t,
            "height_m": p.height_m,
            "datum": p.datum,
            "is_event": p.event is not None,
            "event_type": p.event,
            "interpolated": p.interpolated,
            "source_code": source_code,
            "fetched_at": fetched_at,
        }
        for p in points
    ]
    by_key = {(r["valid_time"], r["datum"], r["is_event"]): r for r in rows}
    rows = list(by_key.values())
    stats = {"inserted": 0, "updated": 0, "removed": 0}
    datums = sorted({str(r["datum"]) for r in rows})
    for i in range(0, len(rows), UPSERT_CHUNK):
        chunk = rows[i : i + UPSERT_CHUNK]
        base = insert(TidePrediction).values(chunk)
        upsert = base.on_conflict_do_update(
            constraint=UNIQUE_CONSTRAINT,
            set_={
                name: base.excluded[name]
                for name in ("height_m", "event_type", "interpolated", "source_code", "fetched_at")
            },
        )
        inserted: list[bool] = list(
            db.scalars(upsert.returning(literal_column("(xmax = 0)", Boolean))).all()
        )
        for was_insert in inserted:
            stats["inserted" if was_insert else "updated"] += 1
    if datums:
        keys = [(r["valid_time"], r["is_event"]) for r in rows]
        stale = delete(TidePrediction).where(
            TidePrediction.station_id == station.id,
            TidePrediction.valid_time >= start,
            TidePrediction.valid_time <= end,
            TidePrediction.datum.in_(datums),
            ~tuple_(TidePrediction.valid_time, TidePrediction.is_event).in_(keys),
        )
        stats["removed"] = db.execute(stale).rowcount or 0  # type: ignore[attr-defined]
    return stats


def _note(station: TideStation, **values: Any) -> None:
    station.metadata_ = {**(station.metadata_ or {}), **values}


def _check_coops_station(
    client: coops.CoopsClient, station: TideStation, now: datetime, warnings: list[str]
) -> None:
    """Refresh kind, reference station and datums from CO-OPS at most once per
    STATION_CHECK_INTERVAL (many subordinate stations publish no datums: kept empty)."""
    checked = (station.metadata_ or {}).get("coops_checked_at")
    if checked and now - datetime.fromisoformat(checked) < STATION_CHECK_INTERVAL:
        return
    try:
        listed = client.stations().get(station.code)
    except ProviderError as exc:
        warnings.append(f"{station.code}: station list unavailable ({exc}); catalog kind kept")
        return
    if listed is None:
        warnings.append(f"{station.code}: not in CO-OPS's tide prediction station list")
        return
    if listed.kind != station.kind or listed.reference_code != station.reference_code:
        warnings.append(
            f"{station.code}: CO-OPS lists it as {listed.kind} (reference "
            f"{listed.reference_code}); the catalog said {station.kind}"
        )
        station.kind = listed.kind
        station.reference_code = listed.reference_code
    try:
        datums = client.datums(station.code)
    except ProviderError as exc:
        warnings.append(f"{station.code}: datums unavailable ({exc})")
    else:
        if datums:
            station.datums = datums
    _note(station, coops_checked_at=now.isoformat())


def _acquire_coops(
    db: Session,
    stations: list[TideStation],
    start: datetime,
    end: datetime,
    now: datetime,
    settings: Settings,
    http: httpx.Client | None,
    backoff_seconds: float,
) -> dict[str, Any]:
    client = coops.CoopsClient(settings, http, backoff_seconds=backoff_seconds)
    out: dict[str, Any] = {"stations": {}, "warnings": []}
    consecutive_transient = 0
    for station in stations:
        entry: dict[str, Any] = {"kind": station.kind}
        out["stations"][station.code] = entry
        if consecutive_transient >= 2:
            entry["error"] = "skipped: CO-OPS unreachable"
            entry["transient"] = True
            continue
        try:
            _check_coops_station(client, station, now, out["warnings"])
            kind: coops.StationKind = "harmonic" if station.kind == "harmonic" else "subordinate"
            events = client.predictions(station.code, start, end, "hilo", "MLLW", kind=kind)
            if kind == "harmonic":
                curve = client.predictions(station.code, start, end, "30", "MLLW", kind=kind)
            else:
                curve = coops.cosine_curve(events, CURVE_STEP, start)
            if not events:
                raise ProviderError(f"noaa_coops: no high/low waters for {station.code}")
            entry.update(
                store_predictions(
                    db,
                    station,
                    [*curve, *events],
                    source_code=coops.SOURCE,
                    start=start,
                    end=end,
                    fetched_at=now,
                )
            )
            entry.update(events=len(events), curve=len(curve), interpolated=kind == "subordinate")
            _note(station, last_acquired_at=now.isoformat(), last_error=None)
            db.commit()
            consecutive_transient = 0
        except ProviderError as exc:
            db.rollback()
            consecutive_transient = consecutive_transient + 1 if exc.transient else 0
            entry["error"] = str(exc)[:300]
            entry["transient"] = exc.transient
            logger.warning("Tide predictions for CO-OPS %s failed: %s", station.code, exc)
            station = db.merge(station)
            _note(station, last_error=str(exc)[:300], last_error_at=now.isoformat())
            db.commit()
    out["requests"] = client.requests
    out["bytes_downloaded"] = client.bytes_downloaded
    if not out["warnings"]:
        del out["warnings"]
    return out


def _acquire_model(
    db: Session, stations: list[TideStation], start: datetime, end: datetime, now: datetime
) -> dict[str, Any]:
    out: dict[str, Any] = {"stations": {}}
    try:
        constants = harmonic_model.load_constants()
    except harmonic_model.ConstantsError as exc:
        for station in stations:
            out["stations"][station.code] = {"error": str(exc)[:300], "transient": False}
        return out
    times = harmonic_model.regular_times(start, end, CURVE_STEP)
    for station in stations:
        slug = station.code.split(":", 1)[-1]
        spot = constants.get(slug)
        entry: dict[str, Any] = {}
        out["stations"][station.code] = entry
        if spot is None:
            entry.update(error=f"no harmonic constants for {slug}", transient=False)
            continue
        heights = harmonic_model.predict(spot, times)
        curve = [
            coops.TidePoint(t, round(float(h), 3), spot.datum)
            for t, h in zip(times, heights, strict=True)
        ]
        events = [
            coops.TidePoint(t, h, spot.datum, "high" if kind == "high" else "low")
            for t, h, kind in harmonic_model.predict_events(spot, start, end)
        ]
        entry.update(
            store_predictions(
                db,
                station,
                [*curve, *events],
                source_code=spot.source,
                start=start,
                end=end,
                fetched_at=now,
            )
        )
        entry.update(events=len(events), curve=len(curve))
        _note(station, last_acquired_at=now.isoformat(), last_error=None)
        db.commit()
    return out


def acquire_tides(
    db: Session,
    now: datetime | None = None,
    *,
    client: httpx.Client | None = None,
    backoff_seconds: float = 1.0,
) -> dict[str, Any]:
    """Fetch or compute predictions for TIDE_HORIZON_DAYS; a summary for the job log."""
    settings = get_settings()
    providers = settings.tide_provider_list
    if not providers:
        return {"status": "skipped", "reason": "TIDE_PROVIDERS is empty"}
    now = (now or utcnow()).astimezone(UTC)
    catalog = seed_tide_catalog(db)
    db.commit()
    start, end = acquisition_window(now, settings)
    result: dict[str, Any] = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "catalog": catalog,
        "providers": {},
    }
    ok = failed = 0
    transient = False
    for provider in providers:
        stations = _linked_stations(db, provider)
        if not stations:
            result["providers"][provider] = {"skipped": "no stations linked to active spots"}
            continue
        if provider == "noaa_coops":
            summary = _acquire_coops(
                db, stations, start, end, now, settings, client, backoff_seconds
            )
        elif provider == "harmonic_model":
            summary = _acquire_model(db, stations, start, end, now)
        else:
            result["providers"][provider] = {"error": f"unknown tide provider {provider!r}"}
            failed += 1
            continue
        result["providers"][provider] = summary
        for entry in summary["stations"].values():
            if "error" in entry:
                failed += 1
                transient = transient or bool(entry.get("transient"))
            else:
                ok += 1
    result["stations_ok"], result["stations_failed"] = ok, failed
    result["status"] = "ok" if not failed else ("partial" if ok else "failed")
    if failed and not ok:
        message = f"no tide station could be updated ({failed} failed)"
        if transient:
            raise TransientProviderError(message)
        raise ProviderError(message)
    return result


def stored_window(db: Session, station_id: int) -> tuple[datetime | None, datetime | None]:
    """First and last stored prediction time of a station."""
    first, last = db.execute(
        select(func.min(TidePrediction.valid_time), func.max(TidePrediction.valid_time)).where(
            TidePrediction.station_id == station_id
        )
    ).one()
    return first, last
