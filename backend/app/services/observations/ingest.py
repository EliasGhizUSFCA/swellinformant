"""Buoy observation ingestion (Celery task ``observations.ingest`` -> :func:`ingest_all`).

* :func:`ingest_all` ingests the last ``OBSERVATION_LOOKBACK_HOURS`` for every provider in
  ``OBSERVATION_PROVIDERS``; one failing provider never stops the others, and failures
  are reported (transient vs permanent) in the result. Only when *every* provider fails
  does the job itself fail (so the job log shows it).
* :func:`ingest_range` ingests one provider for an arbitrary window (backfills).

Writes are idempotent upserts on ``(station_id, observed_at)`` governed by QC precedence
(see :mod:`app.services.observations.records`): a record is skipped when a better-QC'd
record of the same station exists within ``DUPLICATE_WINDOW``; lower-QC'd records in the
window are deleted; at the same timestamp the ON CONFLICT update only fires when the
incoming QC rank is higher than the stored one, or equal with different values, so
``not_qc`` can never overwrite ``good`` even with concurrent writers, and re-ingesting
the same window reports its rows as ``unchanged`` rather than updated. Values outside the
table's CHECK constraints were already dropped (and counted) by the parsers.
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import and_, case, delete, literal_column, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.models import BuoyObservation, BuoyStation
from app.services.http import ProviderError, TransientProviderError
from app.services.observations.records import (
    DUPLICATE_WINDOW,
    QC_RANK,
    ObservationRecord,
    ProviderFetch,
    StationRef,
    resolve_batch,
)
from app.services.observations.stations import stations_for_provider

logger = logging.getLogger(__name__)

PROVIDER_NAMES = ("aodn", "metoffice", "ndbc")
UPSERT_CHUNK = 2000
UPDATABLE = (
    "hs_m",
    "hs_kind",
    "tp_s",
    "tm_s",
    "tm_kind",
    "peak_direction_deg",
    "mean_direction_deg",
    "swell_hs_m",
    "swell_tp_s",
    "swell_direction_deg",
    "windsea_hs_m",
    "wind_speed_kmh",
    "wind_direction_deg",
    "qc_flag",
    "dataset",
    "ingested_at",
)
# Columns whose change makes a same-rank record an update (ingested_at always differs).
COMPARED = tuple(name for name in UPDATABLE if name != "ingested_at")


def build_provider(
    name: str,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
    *,
    backoff_seconds: float = 1.0,
    datasets: Sequence[str] | None = None,
) -> Any:
    """Provider adapter by name. ``datasets`` (AODN only) restricts the datasets read."""
    if datasets is not None and name != "aodn":
        raise ValueError(f"datasets can only be chosen for the aodn provider, not {name!r}")
    if name == "aodn":
        from app.services.observations.aodn import AodnProvider

        return AodnProvider(settings, client, backoff_seconds=backoff_seconds, datasets=datasets)
    if name == "metoffice":
        from app.services.observations.metoffice import MetOfficeProvider

        return MetOfficeProvider(settings, client, backoff_seconds=backoff_seconds)
    if name == "ndbc":
        from app.services.observations.ndbc import NdbcProvider

        return NdbcProvider(settings, client, backoff_seconds=backoff_seconds)
    raise ValueError(f"Unknown observation provider {name!r} (known: {list(PROVIDER_NAMES)})")


@dataclass
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    # Incoming records dropped because a better-QC'd record exists (batch or database).
    superseded_incoming: int = 0
    # Stored lower-QC'd records within DUPLICATE_WINDOW deleted in favour of new ones.
    superseded_stored: int = 0
    # Incoming records identical to the stored row (a re-ingested window); also counts the
    # rare row refused because a concurrent writer stored a better record meanwhile.
    unchanged: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "superseded_incoming": self.superseded_incoming,
            "superseded_stored": self.superseded_stored,
        }


def _rank_sql(column: Any) -> Any:
    return case(QC_RANK, value=column, else_=0)


def upsert_observations(
    db: Session, station_id: int, records: list[ObservationRecord]
) -> UpsertStats:
    """Store one station's records with QC precedence (see module docstring)."""
    stats = UpsertStats()
    records, dropped = resolve_batch(records)
    stats.superseded_incoming += dropped
    now = utcnow()
    for i in range(0, len(records), UPSERT_CHUNK):
        chunk = records[i : i + UPSERT_CHUNK]
        lo = chunk[0].observed_at - DUPLICATE_WINDOW
        hi = chunk[-1].observed_at + DUPLICATE_WINDOW
        existing = db.execute(
            select(BuoyObservation.id, BuoyObservation.observed_at, BuoyObservation.qc_flag)
            .where(
                BuoyObservation.station_id == station_id,
                BuoyObservation.observed_at > lo,
                BuoyObservation.observed_at < hi,
            )
            .order_by(BuoyObservation.observed_at)
        ).all()
        times = [row.observed_at for row in existing]
        rows: list[dict[str, Any]] = []
        delete_ids: set[int] = set()
        for rec in chunk:
            left = bisect.bisect_right(times, rec.observed_at - DUPLICATE_WINDOW)
            right = bisect.bisect_left(times, rec.observed_at + DUPLICATE_WINDOW)
            near = existing[left:right]
            if any(QC_RANK.get(e.qc_flag, 0) > rec.rank for e in near):
                stats.superseded_incoming += 1
                continue
            delete_ids.update(
                e.id
                for e in near
                if QC_RANK.get(e.qc_flag, 0) < rec.rank and e.observed_at != rec.observed_at
            )
            rows.append({**rec.as_row(station_id), "ingested_at": now})
        if delete_ids:
            db.execute(delete(BuoyObservation).where(BuoyObservation.id.in_(delete_ids)))
            stats.superseded_stored += len(delete_ids)
        if not rows:
            continue
        base = insert(BuoyObservation).values(rows)
        table = BuoyObservation.__table__.c
        stored_rank, incoming_rank = _rank_sql(table.qc_flag), _rank_sql(base.excluded.qc_flag)
        changed = or_(*(table[name].is_distinct_from(base.excluded[name]) for name in COMPARED))
        upsert = base.on_conflict_do_update(
            index_elements=["station_id", "observed_at"],
            set_={name: base.excluded[name] for name in UPDATABLE},
            where=or_(stored_rank < incoming_rank, and_(stored_rank == incoming_rank, changed)),
        )
        returned: list[Any] = list(
            db.execute(upsert.returning(literal_column("(xmax = 0)").label("inserted"))).all()
        )
        for (was_insert,) in returned:
            if was_insert:
                stats.inserted += 1
            else:
                stats.updated += 1
        # Rows the WHERE clause refused return nothing: identical to the stored row, or
        # (rare) a concurrent writer stored a better-QC'd record after the check above.
        stats.unchanged += len(rows) - len(returned)
    return stats


def _station_ref(station: BuoyStation) -> StationRef:
    meta = station.metadata_ or {}
    return StationRef(
        code=station.code,
        provider_id=str(meta.get("provider_id") or station.code.split(":", 1)[-1]),
        latitude=station.latitude,
        longitude=station.longitude,
        metadata=meta,
    )


def _apply_station_metadata(
    station: BuoyStation, fetched: Any, stored_until: datetime | None
) -> None:
    meta = dict(station.metadata_ or {})
    if stored_until is not None:
        prev = meta.get("last_observation_at")
        if prev is None or stored_until.isoformat() > prev:
            meta["last_observation_at"] = stored_until.isoformat()
    if fetched.last_position:
        meta["last_reported_position"] = fetched.last_position
    updates = fetched.station_updates
    if updates.get("latitude") is not None and updates.get("longitude") is not None:
        station.latitude = float(updates["latitude"])
        station.longitude = float(updates["longitude"])
        if updates.get("name"):
            station.name = str(updates["name"])[:160]
        meta["provider_position"] = {
            "latitude": station.latitude,
            "longitude": station.longitude,
            "name": updates.get("name"),
            "source": updates.get("position_source", "provider metadata"),
            "updated_at": utcnow().isoformat(),
        }
        meta["position_source"] = meta["provider_position"]["source"]
        meta["position_verified"] = True
    station.metadata_ = meta


def ingest_range(
    db: Session,
    provider: str,
    start: datetime,
    end: datetime,
    stations: list[str] | None = None,
    *,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
    backoff_seconds: float = 1.0,
    datasets: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Ingest one provider's observations in [start, end] for its active stations (or the
    given station codes, active or not). Raises ProviderError if the provider as a whole
    failed; per-station problems are reported in the result. ``datasets`` (AODN only)
    restricts the datasets read, e.g. ``["wave_buoy_delayed_qc"]`` for a QC'd-only
    backfill."""
    if provider not in PROVIDER_NAMES:
        raise ValueError(
            f"Unknown observation provider {provider!r} (known: {list(PROVIDER_NAMES)})"
        )
    if datasets is not None and provider != "aodn":
        raise ValueError(f"datasets can only be chosen for the aodn provider, not {provider!r}")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if start >= end:
        raise ValueError("start must be before end")
    rows = stations_for_provider(db, provider, stations)
    result: dict[str, Any] = {
        "provider": provider,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "stations": {},
    }
    if datasets is not None:
        result["datasets"] = list(datasets)
    if stations:
        unknown = sorted(set(stations) - {s.code for s in rows})
        if unknown:
            result["unknown_stations"] = unknown
    if not rows:
        result["message"] = "no stations for this provider (run `obs seed-stations`?)"
        return result
    adapter = build_provider(
        provider, settings, client, backoff_seconds=backoff_seconds, datasets=datasets
    )
    fetch: ProviderFetch = adapter.fetch([_station_ref(s) for s in rows], start, end)
    totals = {"records": 0, "inserted": 0, "updated": 0, "unchanged": 0, "errors": 0}
    for station in rows:
        fetched = fetch.station(station.code)
        upsert = upsert_observations(db, station.id, fetched.records)
        stored = [r.observed_at for r in fetched.records if r.qc_flag != "bad"]
        _apply_station_metadata(station, fetched, max(stored) if stored else None)
        db.commit()
        latest = db.scalar(
            select(BuoyObservation.observed_at)
            .where(BuoyObservation.station_id == station.id, BuoyObservation.qc_flag != "bad")
            .order_by(BuoyObservation.observed_at.desc())
            .limit(1)
        )
        result["stations"][station.code] = {
            "files": fetched.files,
            "records": len(fetched.records),
            **upsert.as_dict(),
            "parse": fetched.stats.as_dict(),
            "errors": fetched.errors[:20],
            "latest_observation": latest.isoformat() if latest else None,
        }
        totals["records"] += len(fetched.records)
        totals["inserted"] += upsert.inserted
        totals["updated"] += upsert.updated
        totals["unchanged"] += upsert.unchanged
        totals["errors"] += len(fetched.errors)
    result["requests"] = fetch.requests
    result["totals"] = totals
    if fetch.warnings:
        result["warnings"] = fetch.warnings[:20]
    return result


def ingest_all(
    db: Session,
    now: datetime | None = None,
    *,
    client: httpx.Client | None = None,
    backoff_seconds: float = 1.0,
) -> dict[str, Any]:
    """Job entry point: recent observations from every configured provider."""
    settings = get_settings()
    if not settings.observations_enabled:
        return {"skipped": "OBSERVATIONS_ENABLED is false"}
    end = (now or utcnow()).astimezone(UTC)
    start = end - timedelta(hours=settings.observation_lookback_hours)
    providers: dict[str, Any] = {}
    failed: dict[str, dict[str, Any]] = {}
    for name in settings.observation_provider_list:
        try:
            providers[name] = ingest_range(
                db, name, start, end, client=client, backoff_seconds=backoff_seconds
            )
        except ProviderError as exc:
            db.rollback()
            logger.warning("Observation provider %s failed: %s", name, exc)
            failed[name] = {"error": str(exc)[:500], "transient": exc.transient}
        except ValueError as exc:
            db.rollback()
            failed[name] = {"error": str(exc)[:500], "transient": False}
        except Exception as exc:  # a bug in one adapter must not stop the others
            db.rollback()
            logger.exception("Observation provider %s crashed", name)
            failed[name] = {"error": f"{type(exc).__name__}: {exc}"[:500], "transient": False}
    result = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "providers": providers,
        "failed": failed,
        "status": "partial" if failed and providers else ("failed" if failed else "ok"),
    }
    if failed and not providers:
        summary = "; ".join(f"{k}: {v['error']}" for k, v in failed.items())
        if any(v["transient"] for v in failed.values()):
            raise TransientProviderError(f"every observation provider failed: {summary}")
        raise ProviderError(f"every observation provider failed: {summary}")
    return result
