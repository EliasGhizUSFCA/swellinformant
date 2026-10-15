"""The tide and observation blocks for one spot (contract: app/schemas/tides.py).

Tide block, in this order:

1. ``available``: the stored predictions (``tides.acquire``) of the spot's first tide link
   (primary, then fallback; only providers in TIDE_PROVIDERS) that has any for the window.
   One station per block: sources and datums are never mixed in one curve. The curve
   covers the window with one point beyond each end (so a chart and the per-step heights
   can be drawn to the edges); the events are the high and low waters in the window plus
   the one before and the one after it (so rising/falling is known at both ends).
   ``reason`` then carries the caveat the surfer should read with the curve: a model tide
   is "relative to model mean sea level, not a chart datum" (plus the catalog's caveat for
   reefs, passes and estuaries), and a subordinate station's curve is interpolated
   between its published high and low waters.
2. ``demo``: no stored predictions, but the spot's current forecast is the synthetic demo
   provider's (FORECAST_PROVIDERS=demo): its synthetic sea level, labelled as such, with
   no datum and no station.
3. ``unavailable`` with the reason (no tide source for the spot, or nothing stored yet).

The tide preference text comes from the spot profile, its tide research class (the
current research assessment) and the profile decision for the tide preference: the
preference is shown only when it is set, the class is moderate or high and the decision is
not "disputed" (the rule the quality score uses), otherwise
:data:`TIDE_PREFERENCE_NOT_ESTABLISHED`. No tide rule is made up here.

Observation block: the latest measurement of each offshore_reference or
nearshore_reference station paired with the spot in data/observation_stations.json
(regional and historical stations are not listed). Every item is an instrument
measurement with its station, distance and time; one older than its provider's limit
(STALE_AFTER) is flagged ``stale``. A station without a wave reading in the window is
shown with its last one, flagged stale (e.g. a buoy whose wave sensor stopped).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, cast

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import (
    BuoyObservation,
    BuoyStation,
    SurfSpot,
    TidePrediction,
    TideStation,
    WaveForecast,
)
from app.schemas.tides import (
    TIDE_PREFERENCE_NOT_ESTABLISHED,
    ObservationOut,
    ObservationsBlockOut,
    TideBlockOut,
    TideEventOut,
    TidePointOut,
    TidePreferenceOut,
    TideStationKind,
    TideStationOut,
)
from app.services.research.assess import current_assessment
from app.services.research.decisions import (
    DecisionValidationError,
    default_decisions_path,
    load_decisions,
    record_for,
)
from app.services.tides import catalog, harmonic_model

logger = logging.getLogger(__name__)

NO_TIDE_SOURCE = "No tide source configured for this spot."
NO_OBSERVATIONS = "Observations are not available for this spot yet."
NOT_STORED = "No tide predictions are stored for this period yet."
NO_REFERENCE_STATIONS = "No measuring station near this spot is paired with it."
NO_RECENT_READINGS = "The paired stations have no stored readings yet."
ALL_STALE = "The paired stations have not reported recently."
DEMO_REASON = "Synthetic tide from the demo forecast provider, not a prediction for this place."
DEMO_ATTRIBUTION = "Synthetic demo data"
MODEL_NOTE = "Model tide (EOT20), relative to model mean sea level, not a chart datum."
SUBORDINATE_NOTE = (
    "CO-OPS publishes only high and low waters for this station; the curve between them is "
    "interpolated here."
)

# Tide research classes under which the profile's tide preference is shown (the same rule
# as the quality score's).
ESTABLISHED_CLASSES = frozenset({"moderate", "high"})
PREFERENCE_TEXT = {
    "low": "Best around low tide.",
    "low_to_mid": "Best from low to mid tide.",
    "mid": "Best around mid tide.",
    "mid_to_high": "Best from mid to high tide.",
    "high": "Best around high tide.",
    "all": "Works on all tides.",
}

# The block's curve reaches one stored point beyond each end of the window (the curve is
# 30-minute spaced, the demo curve hourly) and its events one beyond each end.
CURVE_MARGIN = timedelta(hours=1)
EVENT_MARGIN = timedelta(hours=14)

# Observations older than this (hours) are stale: providers publish hourly or half-hourly
# and are ingested hourly; AODN issues each hour about 75 min after it. Heuristic limits.
STALE_AFTER = {"ndbc": 3.0, "metoffice": 3.0, "aodn": 4.0}
STALE_AFTER_DEFAULT = 6.0
LISTED_RELATIONS = ("offshore_reference", "nearshore_reference")


# --------------------------------------------------------------------------- preference
@lru_cache(maxsize=2)
def _decisions_cached(path: str, mtime: float) -> dict[str, list[dict[str, Any]]]:
    return load_decisions(path)


def _decisions() -> dict[str, list[dict[str, Any]]]:
    try:
        path = default_decisions_path()
        return _decisions_cached(str(path), path.stat().st_mtime)
    except (DecisionValidationError, OSError, ValueError) as exc:
        logger.warning("Profile decisions not used for the tide preference: %s", exc)
        return {}


def tide_preference(db: Session, spot: SurfSpot) -> TidePreferenceOut:
    value = spot.tide_preference.value if spot.tide_preference is not None else None
    assessment = current_assessment(db, spot.id)
    tide = (assessment.per_variable or {}).get("tide") if assessment is not None else None
    research_class = str(tide["class"]) if isinstance(tide, dict) and tide.get("class") else None
    record = record_for(_decisions(), spot.slug, "tide_preference")
    decision = str(record["decision"]) if record is not None else None
    established = (
        value is not None and decision != "disputed" and research_class in ESTABLISHED_CLASSES
    )
    return TidePreferenceOut(
        value=value,
        research_class=research_class,
        decision=decision,
        text=PREFERENCE_TEXT.get(value or "", TIDE_PREFERENCE_NOT_ESTABLISHED)
        if established
        else TIDE_PREFERENCE_NOT_ESTABLISHED,
    )


# --------------------------------------------------------------------------- tide block
def _station_note(station: TideStation, caveat: str | None) -> str | None:
    if station.kind == "model":
        return " ".join(x for x in (MODEL_NOTE, caveat) if x)
    parts: list[str] = []
    msl = (station.datums or {}).get("MSL")
    if isinstance(msl, int | float):
        parts.append(f"Heights above MLLW; mean sea level is {msl:.2f} m above MLLW here.")
    if station.kind == "subordinate":
        parts.append(SUBORDINATE_NOTE)
    return " ".join(parts) or None


def _attribution(station: TideStation) -> str | None:
    if station.attribution and station.licence:
        return f"{station.attribution} ({station.licence})"
    return station.attribution or station.licence


def _stored_block(
    db: Session,
    spot: SurfSpot,
    station: TideStation,
    distance_km: float,
    caveat: str | None,
    start: datetime,
    end: datetime,
    preference: TidePreferenceOut,
) -> TideBlockOut | None:
    rows = db.execute(
        select(
            TidePrediction.valid_time,
            TidePrediction.height_m,
            TidePrediction.datum,
            TidePrediction.is_event,
            TidePrediction.event_type,
            TidePrediction.interpolated,
            TidePrediction.source_code,
            TidePrediction.fetched_at,
        )
        .where(
            TidePrediction.station_id == station.id,
            TidePrediction.valid_time >= start - EVENT_MARGIN,
            TidePrediction.valid_time <= end + EVENT_MARGIN,
        )
        .order_by(TidePrediction.valid_time)
    ).all()
    if not any(start <= r.valid_time <= end for r in rows):
        return None
    datum = rows[0].datum
    if any(r.datum != datum for r in rows):
        # One datum per block; the job stores one per station, so this is a bad state.
        logger.error("Tide station %s has mixed datums; showing %s only", station.code, datum)
        rows = [r for r in rows if r.datum == datum]
    curve = [
        r
        for r in rows
        if not r.is_event and start - CURVE_MARGIN <= r.valid_time <= end + CURVE_MARGIN
    ]
    events = [r for r in rows if r.is_event]
    inside = [e for e in events if start <= e.valid_time <= end]
    before = [e for e in events if e.valid_time < start][-1:]
    after = [e for e in events if e.valid_time > end][:1]
    used = [*curve, *before, *inside, *after]
    return TideBlockOut(
        status="available",
        reason=_station_note(station, caveat),
        station=TideStationOut(
            code=station.code,
            name=station.name or station.code,
            provider=station.provider,
            kind=cast(TideStationKind, station.kind or "model"),
            distance_km=round(distance_km, 1),
        ),
        source=rows[0].source_code,
        datum=datum,
        curve=[
            TidePointOut(t=r.valid_time, height_m=r.height_m, interpolated=r.interpolated)
            for r in curve
        ],
        events=[
            TideEventOut(t=e.valid_time, kind=e.event_type, height_m=e.height_m)
            for e in (*before, *inside, *after)
        ],
        preference=preference,
        attribution=_attribution(station),
        fetched_at=max(r.fetched_at for r in used) if used else None,
    )


def _demo_block(
    db: Session,
    spot: SurfSpot,
    start: datetime,
    end: datetime,
    now: datetime,
    preference: TidePreferenceOut,
) -> TideBlockOut | None:
    if "demo" not in get_settings().forecast_provider_list:
        return None
    # Imported here: the forecast query helpers pull in the provider registry.
    from app.services.forecasts.queries import latest_runs_by_spot

    run = latest_runs_by_spot(db, now).get(spot.id)
    if run is None or not run.source.is_demo:
        return None
    rows = db.execute(
        select(WaveForecast.valid_time, WaveForecast.sea_level_m)
        .where(
            WaveForecast.run_id == run.id,
            WaveForecast.spot_id == spot.id,
            WaveForecast.valid_time >= start - CURVE_MARGIN,
            WaveForecast.valid_time <= end + CURVE_MARGIN,
            WaveForecast.sea_level_m.isnot(None),
        )
        .order_by(WaveForecast.valid_time)
    ).all()
    if len(rows) < 2:
        return None
    times = [r.valid_time for r in rows]
    heights = np.array([float(r.sea_level_m) for r in rows])
    steps = {b - a for a, b in zip(times, times[1:], strict=False)}
    events = harmonic_model.find_extrema(times, heights) if len(steps) == 1 else []
    return TideBlockOut(
        status="demo",
        reason=DEMO_REASON,
        source="demo",
        curve=[
            TidePointOut(t=t, height_m=round(float(h), 3), interpolated=False)
            for t, h in zip(times, heights, strict=True)
        ],
        events=[
            TideEventOut(t=t, kind="high" if k == "high" else "low", height_m=h)
            for t, h, k in events
        ],
        preference=preference,
        attribution=DEMO_ATTRIBUTION,
        fetched_at=run.completed_at,
    )


def spot_tide_block(
    db: Session, spot: SurfSpot, start: datetime, end: datetime, now: datetime | None = None
) -> TideBlockOut:
    """Predicted tide curve and high/low waters at the spot's station for [start, end)."""
    now = (now or utcnow()).astimezone(UTC)
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    preference = tide_preference(db, spot)
    providers = set(get_settings().tide_provider_list)
    links = [(lk, st) for lk, st in catalog.spot_links(db, spot.id) if st.provider in providers]
    for link, station in links:
        caveat = (station.metadata_ or {}).get("caveat")
        block = _stored_block(db, spot, station, link.distance_km, caveat, start, end, preference)
        if block is not None:
            return block
    demo = _demo_block(db, spot, start, end, now, preference)
    if demo is not None:
        return demo
    return TideBlockOut(
        status="unavailable",
        reason=NOT_STORED if links else _no_source_reason(spot),
        preference=preference,
    )


def _no_source_reason(spot: SurfSpot) -> str:
    try:
        entry = catalog.load_catalog().get("spots", {}).get(spot.slug) or {}
    except (OSError, ValueError):
        return NO_TIDE_SOURCE
    reason = entry.get("unavailable")
    return str(reason) if reason else NO_TIDE_SOURCE


# --------------------------------------------------------------------------- observations
def _paired(db: Session, slug: str) -> list[tuple[BuoyStation, dict[str, Any]]]:
    stations = db.scalars(
        select(BuoyStation).where(BuoyStation.metadata_["paired_spots"].isnot(None))
    ).all()
    out: list[tuple[BuoyStation, dict[str, Any]]] = []
    for station in stations:
        for pair in (station.metadata_ or {}).get("paired_spots") or []:
            if pair.get("slug") == slug and pair.get("relation") in LISTED_RELATIONS:
                out.append((station, pair))
    out.sort(key=lambda sp: (LISTED_RELATIONS.index(sp[1]["relation"]), sp[1]["distance_km"]))
    return out


def _latest(db: Session, station_id: int, end: datetime) -> BuoyObservation | None:
    """The newest wave reading at or before ``end`` (else the newest reading at all)."""
    base = select(BuoyObservation).where(
        BuoyObservation.station_id == station_id,
        BuoyObservation.observed_at <= end,
        BuoyObservation.qc_flag != "bad",
    )
    for query in (base.where(BuoyObservation.hs_m.isnot(None)), base):
        row = db.scalar(query.order_by(BuoyObservation.observed_at.desc()).limit(1))
        if row is not None:
            return row
    return None


def stale_after_hours(provider: str) -> float:
    return STALE_AFTER.get(provider, STALE_AFTER_DEFAULT)


def spot_observations_block(
    db: Session, spot: SurfSpot, start: datetime, end: datetime, now: datetime | None = None
) -> ObservationsBlockOut:
    """Measured conditions at the spot's offshore and nearshore reference stations."""
    now = (now or utcnow()).astimezone(UTC)
    paired = _paired(db, spot.slug)
    if not paired:
        return ObservationsBlockOut(status="unavailable", reason=NO_REFERENCE_STATIONS)
    items: list[ObservationOut] = []
    for station, pair in paired:
        obs = _latest(db, station.id, end)
        if obs is None:
            continue
        age_h = (now - obs.observed_at).total_seconds() / 3600.0
        stale = obs.observed_at < start or age_h > stale_after_hours(station.provider)
        items.append(
            ObservationOut(
                t=obs.observed_at,
                kind="instrument",
                station_code=station.code,
                station_name=station.name,
                provider=station.provider,
                relation=str(pair["relation"]),
                distance_km=float(pair["distance_km"]),
                hs_m=obs.hs_m,
                hs_kind=obs.hs_kind if obs.hs_m is not None else None,
                tp_s=obs.tp_s,
                dir_from_deg=obs.peak_direction_deg
                if obs.peak_direction_deg is not None
                else obs.mean_direction_deg,
                wind_kmh=obs.wind_speed_kmh,
                qc_flag=obs.qc_flag,
                stale=stale,
                licence=station.license or None,
                attribution=station.attribution or None,
            )
        )
    if not items:
        return ObservationsBlockOut(status="unavailable", reason=NO_RECENT_READINGS)
    if all(item.stale for item in items):
        return ObservationsBlockOut(status="stale", reason=ALL_STALE, items=items)
    return ObservationsBlockOut(status="available", items=items)
