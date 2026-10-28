"""Read models for spots, forecasts and events (used by the API).

Every read serves the run the published forecast version maps each spot to
(``queries.served_runs_by_spot``; before the first publication, the legacy selection) and
reports that spot's ``freshness`` (``serving.freshness_by_spot``, design D10).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Hashable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, cast, get_args
from zoneinfo import ZoneInfo

from sqlalchemy import Row, Select, func, select, tuple_
from sqlalchemy.orm import Session, contains_eager, lazyload, object_session

from app.core.database import utcnow
from app.models import (
    ForecastComparison,
    ForecastEnsembleStat,
    ForecastRun,
    ForecastSource,
    SurfQualityPrediction,
    SurfSpot,
    SwellEvent,
    WaveForecast,
)
from app.models.enums import QualityLabel, SwellEventStatus
from app.schemas.forecasts import SpotFreshnessOut
from app.schemas.spots import (
    AirportOut,
    AttributionLinkOut,
    BlendInfo,
    Conditions,
    DailySummary,
    EnsemblePoint,
    EventOut,
    ForecastPoint,
    ForecastRunOut,
    ForecastSourceOut,
    GustKind,
    MapSnapshotOut,
    MapSpotOut,
    ModelPoint,
    ModelSeries,
    PhysicalProfileOut,
    RunMemberOut,
    SpotAirportOut,
    SpotDetail,
    SpotForecastOut,
    SpotMini,
    SpotSummary,
    WindFavourability,
    WindStrength,
)
from app.services import timesteps
from app.services.forecasts.base import EXTRA_GUST_KIND
from app.services.forecasts.queries import (
    USABLE,
    is_stale,
    run_id_is_stale,
    served_runs_by_spot,
    served_version,
    source_attribution_links,
)
from app.services.forecasts.registry import configured_source_codes
from app.services.forecasts.serving import freshness_by_spot
from app.services.notifications.alerts import STALE_ALERT_FACTOR
from app.services.research.views import research_classes, research_out
from app.services.surf_quality.breaking import dominant_component
from app.services.surf_quality.forecast_confidence import api_fields
from app.services.surf_quality.outputs import hour_outputs
from app.services.surf_quality.scoring import label_for_score, min_score_for_label
from app.services.surf_quality.sun import daylight_windows
from app.services.swell_detection.service import peak_confidence, peak_wind

MODEL_SERIES_STEP_HOURS = 3
# The current conditions are the stored step nearest to now, searched this far back (the
# longest step any source uses: ECMWF and GEFS are 6-hourly at long leads).
CURRENT_LOOKBACK = timedelta(hours=6)
# The best upcoming hour may be up to this far in the past (it is still today's surf).
BEST_LOOKBACK = timedelta(hours=1)
_WIND_STRENGTHS = frozenset(get_args(WindStrength))
_WIND_FAVOURABILITY = frozenset(get_args(WindFavourability))
_GUST_KINDS = frozenset(get_args(GustKind))


def wind_fields(components: dict[str, Any] | None, extra: dict[str, Any] | None) -> dict[str, Any]:
    """Wind strength and favourability from the prediction's stored ``wind_detail``
    (scoring 2.1.0; None for older rows) and the gust definition the record names."""
    detail = (components or {}).get("wind_detail") or {}
    strength = detail.get("strength") if isinstance(detail, dict) else None
    favourability = detail.get("favourability") if isinstance(detail, dict) else None
    gust_kind = (extra or {}).get(EXTRA_GUST_KIND)
    return {
        "wind_strength": strength if strength in _WIND_STRENGTHS else None,
        "wind_favourability": favourability if favourability in _WIND_FAVOURABILITY else None,
        "gust_kind": gust_kind if gust_kind in _GUST_KINDS else None,
    }


def spot_mini(spot: SurfSpot) -> SpotMini:
    return SpotMini(
        id=spot.id,
        slug=spot.slug,
        name=spot.name,
        country=spot.country,
        country_code=spot.country_code,
        region=spot.region,
        region_group=spot.region_group,
        latitude=spot.latitude,
        longitude=spot.longitude,
        timezone=spot.timezone,
        break_type=spot.break_type.value,
    )


AttributionMemo = dict[int, list[AttributionLinkOut]]


def run_attribution_links(
    db: Session, run: ForecastRun, memo: AttributionMemo | None = None
) -> list[AttributionLinkOut]:
    """``queries.source_attribution_links`` (a statement for a blend run), read once per
    run when the caller lists several events of the same run (``memo``)."""
    if memo is None:
        return source_attribution_links(db, run)
    if run.id not in memo:
        memo[run.id] = source_attribution_links(db, run)
    return memo[run.id]


def event_out(
    event: SwellEvent,
    now: datetime | None = None,
    include_history: bool = False,
    *,
    links: AttributionMemo | None = None,
) -> EventOut:
    """``links``: the attribution links already read for the events' runs (a list of events
    from a few runs then reads each run's once)."""
    now = now or utcnow()
    # The read-time staleness rule, as for the same hour in the forecast (_conditions).
    db = object_session(event)
    stale = db is not None and run_id_is_stale(db, event.latest_run_id, now)
    return EventOut(
        id=event.id,
        spot=spot_mini(event.spot),
        status=event.status.value,
        start_time=event.start_time,
        end_time=event.end_time,
        peak_time=event.peak_time,
        peak_score=event.peak_score,
        avg_score=event.avg_score,
        peak_label=event.peak_label.value,
        peak_breaking_height_min_ft=event.peak_breaking_height_min_ft,
        peak_breaking_height_max_ft=event.peak_breaking_height_max_ft,
        peak_swell_height_m=event.peak_swell_height_m,
        peak_swell_period_s=event.peak_swell_period_s,
        peak_swell_direction_deg=event.peak_swell_direction_deg,
        peak_wind_speed_kmh=event.peak_wind_speed_kmh,
        peak_wind_direction_deg=event.peak_wind_direction_deg,
        peak_wind_relation=event.peak_wind_relation,
        qualifying_hours=event.qualifying_hours,
        confidence=event.confidence,
        confidence_label=event.confidence_label.value,
        lead_days=round((event.start_time - now).total_seconds() / 86400, 1),
        version=event.version,
        first_detected_at=event.first_detected_at,
        last_updated_at=event.last_updated_at,
        is_demo=event.is_demo,
        source_code=event.source_code,
        history=event.history if include_history else [],
        stale=stale,
        # The credits its forecast run's sources ask for next to their data (Open-Meteo,
        # ECMWF, NOAA); cards show the swell, wind and our estimate from that run.
        attribution_links=(
            run_attribution_links(db, event.latest_run, links)
            if db is not None and event.latest_run is not None
            else []
        ),
        **peak_confidence(event, stale=stale),
        **peak_wind(event),
    )


def _conditions(
    p: SurfQualityPrediction,
    f: WaveForecast,
    run: ForecastRun | None = None,
    *,
    stale: bool = False,
) -> Conditions:
    """One forecast hour. ``stale``: the run has gone stale since it was scored, so the
    stored confidence classes are capped at Moderate (forecast_confidence.api_fields)."""
    source = run.source if run is not None else None
    main = dominant_component(p, f)  # the component that drives the surf, not "swell 1"
    return Conditions(
        sig_wave_height_m=f.sig_wave_height_m,
        source_code=source.code if source else None,
        source_name=source.name if source else None,
        is_demo=bool(source.is_demo) if source else False,
        run_issued_at=run.issued_at if run is not None else None,
        method=p.breaking_height_method,
        algorithm_version=p.algorithm_version,
        explanation=p.explanation,
        breaking_height_mid_ft=p.breaking_height_mid_ft,
        breaking_p10_ft=p.breaking_p10_ft,
        breaking_p90_ft=p.breaking_p90_ft,
        uncertainty_kind=p.uncertainty_kind,
        ml_model_version=p.ml_model_version,
        valid_time=p.valid_time,
        score=p.score,
        label=p.label.value,
        breaking_height_min_ft=p.breaking_height_min_ft,
        breaking_height_max_ft=p.breaking_height_max_ft,
        swell_height_m=main[0],
        swell_period_s=main[1],
        swell_direction_deg=main[2],
        wind_speed_kmh=f.wind_speed_kmh,
        wind_direction_deg=f.wind_direction_deg,
        wind_relation=p.wind_relation,
        confidence=p.confidence,
        confidence_label=p.confidence_label.value,
        is_daylight=p.is_daylight,
        **api_fields(p, stale=stale),
        **wind_fields(p.components, f.extra),
    )


def _summary_fields(spot: SurfSpot) -> dict[str, object]:
    primary = spot.primary_airport
    return {
        **spot_mini(spot).model_dump(),
        "wave_direction": spot.wave_direction,
        "is_big_wave": spot.is_big_wave,
        "skill_level": spot.skill_level.value,
        "wave_height_min_ft": spot.wave_height_min_ft,
        "wave_height_max_ft": spot.wave_height_max_ft,
        "best_months": list(spot.best_months or []),
        "primary_airport": primary.airport_iata if primary else None,
    }


def _nearest_step(times: Sequence[datetime], now: datetime) -> int | None:
    """Index of the stored step nearest to ``now`` (the earlier one on a tie), provided it
    is at most one step (its own duration) away; None otherwise. The steps are the model's
    native valid times, so nothing between them is invented."""
    if not times:
        return None
    durations = timesteps.step_hours(times)
    best = min(range(len(times)), key=lambda i: (abs((times[i] - now).total_seconds()), i))
    gap_h = abs((times[best] - now).total_seconds()) / 3600.0
    return best if gap_h <= max(durations[best], 0.0) + 1e-9 else None


def predictions_with_forecasts(*criteria: Any) -> Select[SurfQualityPrediction, WaveForecast]:
    """Predictions together with their forecast rows, the forecast read once.

    ``SurfQualityPrediction.forecast`` is ``lazy="joined"`` (the one reader of it, swell
    detection's peak query, wants the forecast in the same statement), so selecting both
    entities over a plain join would add a second, eager copy of every forecast row to the
    statement. ``contains_eager`` fills the relationship from this join instead: one
    statement, each forecast column once, and ``.forecast`` loaded without another query.
    """
    return (
        select(SurfQualityPrediction, WaveForecast)
        .join(SurfQualityPrediction.forecast)
        .options(contains_eager(SurfQualityPrediction.forecast))
        .where(*criteria)
    )


_SHARED_HOURS = "spot_views.shared_hours"
HourRows = list[tuple[SurfQualityPrediction, WaveForecast]]


@contextmanager
def shared_hour_reads(db: Session) -> Iterator[None]:
    """Within the block, :func:`spot_hours` reads each run, spot and window once, and the
    authoritative runs, a run's source and its members are built once: a spot page's
    forecast and timeline, computed together (app.services.public_cache), read the same
    hours with their forecasts (about 0.8 MB for a 10-day blend, docs/map-layers.md)."""
    if _SHARED_HOURS in db.info:
        yield
        return
    db.info[_SHARED_HOURS] = {}
    try:
        yield
    finally:
        db.info.pop(_SHARED_HOURS, None)


def _shared[T](db: Session, key: tuple[Hashable, ...], build: Callable[[], T]) -> T:
    """``build()`` once per key inside :func:`shared_hour_reads`, every time outside it."""
    memo: dict[tuple[Hashable, ...], Any] | None = db.info.get(_SHARED_HOURS)
    if memo is None:
        return build()
    if key not in memo:
        memo[key] = build()
    return cast(T, memo[key])


def spot_hours(db: Session, run_id: int, spot_id: int, start: datetime, end: datetime) -> HourRows:
    """The run's predictions for the spot from ``start`` to ``end`` (both included) with
    their forecasts, in time order (read once per window inside :func:`shared_hour_reads`)."""

    def read() -> HourRows:
        return [
            (p, f)
            for p, f in db.execute(
                predictions_with_forecasts(
                    SurfQualityPrediction.run_id == run_id,
                    SurfQualityPrediction.spot_id == spot_id,
                    SurfQualityPrediction.valid_time >= start,
                    SurfQualityPrediction.valid_time <= end,
                ).order_by(SurfQualityPrediction.valid_time)
            )
        ]

    return _shared(db, ("hours", run_id, spot_id, start, end), read)


def _predictions_by_id(
    db: Session, ids: set[int]
) -> dict[int, tuple[SurfQualityPrediction, WaveForecast]]:
    """The chosen predictions in full with their forecasts (one statement)."""
    if not ids:
        return {}
    rows = db.execute(predictions_with_forecasts(SurfQualityPrediction.id.in_(ids))).all()
    return {p.id: (p, f) for p, f in rows}


def _current_and_best(
    db: Session,
    runs: dict[int, ForecastRun],
    now: datetime,
    horizon_days: int,
    spot_id: int | None = None,
) -> tuple[dict[int, Conditions], dict[int, Conditions]]:
    """Per spot: the current conditions (the stored step nearest to now, within one step)
    and the best daylight hour of the next ``horizon_days``, from each spot's authoritative
    run. ``spot_id`` restricts the query to that spot's predictions.

    Two statements, whatever the number of spots: the hours to choose from are scanned with
    the five columns the choice needs (and only from each spot's own authoritative run), then
    the at most two chosen hours per spot are read in full with their forecasts. Reading every
    hour in full read about 5 kB per hour, 37.5 MB for the 50 spots of a fresh 2026-10-16
    run, to answer with 2 hours a spot (docs/map-layers.md, database egress)."""
    if spot_id is not None:
        runs = {spot_id: runs[spot_id]} if spot_id in runs else {}
    if not runs:
        return {}, {}
    stale = {r.id: is_stale(r, now) for r in runs.values()}
    p = SurfQualityPrediction
    scan = (
        select(p.id, p.spot_id, p.valid_time, p.is_daylight, p.score)
        .where(
            tuple_(p.spot_id, p.run_id).in_([(sid, r.id) for sid, r in runs.items()]),
            p.valid_time >= now - CURRENT_LOOKBACK,
            p.valid_time <= now + timedelta(days=horizon_days),
        )
        .order_by(p.spot_id, p.valid_time)
    )
    by_spot: dict[int, list[Any]] = defaultdict(list)
    for row in db.execute(scan):
        by_spot[row.spot_id].append(row)

    chosen: dict[int, tuple[int | None, int | None]] = {}
    for sid, rows in by_spot.items():
        i = _nearest_step([r.valid_time for r in rows], now)
        top = None
        for r in rows:
            if r.valid_time < now - BEST_LOOKBACK or not r.is_daylight:
                continue
            if top is None or r.score > top.score:
                top = r
        chosen[sid] = (rows[i].id if i is not None else None, top.id if top is not None else None)
    full = _predictions_by_id(db, {i for pair in chosen.values() for i in pair if i is not None})

    current: dict[int, Conditions] = {}
    best: dict[int, Conditions] = {}
    for sid, (current_id, best_id) in chosen.items():
        run = runs[sid]
        # A chosen hour pruned between the two statements is left out, as if never stored.
        if current_id is not None and current_id in full:
            current[sid] = _conditions(*full[current_id], run, stale=stale[run.id])
        if best_id is not None and best_id in full:
            best[sid] = _conditions(*full[best_id], run, stale=stale[run.id])
    return current, best


def _active_events(
    db: Session, now: datetime, spot_id: int | None = None
) -> tuple[dict[int, SwellEvent], dict[int, int]]:
    """Per spot: the active event that starts first (and still runs at ``now``), read in
    full, and the number of such events. The events are scanned by id first, so only one
    event per spot is read in full (with its history), not every active event."""
    stmt = (
        select(SwellEvent.id, SwellEvent.spot_id)
        .where(SwellEvent.status == SwellEventStatus.ACTIVE, SwellEvent.end_time >= now)
        .order_by(SwellEvent.start_time)
    )
    if spot_id is not None:
        stmt = stmt.where(SwellEvent.spot_id == spot_id)
    first: dict[int, Any] = {}
    counts: dict[int, int] = defaultdict(int)
    for event_id, sid in db.execute(stmt):
        first.setdefault(sid, event_id)
        counts[sid] += 1
    if not first:
        return {}, {}
    # The spots are in the session already (every caller loaded them), so the event's
    # ``spot`` comes from the identity map instead of a joined copy of each spot row.
    full = {
        e.id: e
        for e in db.scalars(
            select(SwellEvent)
            .where(SwellEvent.id.in_(list(first.values())))
            .options(lazyload(SwellEvent.spot))
        )
    }
    return {sid: full[eid] for sid, eid in first.items() if eid in full}, dict(counts)


def _summary(
    spot: SurfSpot,
    current: dict[int, Conditions],
    best: dict[int, Conditions],
    events: tuple[dict[int, SwellEvent], dict[int, int]],
    research_class: str | None,
    now: datetime,
    links: AttributionMemo | None = None,
    freshness: SpotFreshnessOut | None = None,
) -> SpotSummary:
    first, counts = events
    upcoming = first.get(spot.id)
    return SpotSummary(
        **_summary_fields(spot),  # type: ignore[arg-type]
        current=current.get(spot.id),
        best_upcoming=best.get(spot.id),
        next_event=event_out(upcoming, now, links=links) if upcoming is not None else None,
        upcoming_event_count=counts.get(spot.id, 0),
        research_class=research_class,
        freshness=freshness,
    )


def spot_summaries(
    db: Session, now: datetime | None = None, horizon_days: int = 7
) -> list[SpotSummary]:
    """All active spots with current conditions, best upcoming window and next event."""
    now = now or utcnow()
    spots = db.scalars(select(SurfSpot).where(SurfSpot.is_active).order_by(SurfSpot.name)).all()
    runs = served_runs_by_spot(db, now)
    current, best = _current_and_best(db, runs, now, horizon_days)
    events = _active_events(db, now)
    research = research_classes(db)  # one query for all spots
    freshness = freshness_by_spot(db, runs, now)
    links: AttributionMemo = {}
    return [
        _summary(s, current, best, events, research.get(s.id), now, links, freshness.get(s.id))
        for s in spots
    ]


def spot_detail(
    db: Session, spot: SurfSpot, now: datetime | None = None, horizon_days: int = 7
) -> SpotDetail:
    """One spot's page data. Only this spot's predictions and events are read; the
    ``current`` and ``best_upcoming`` hours are the ones :func:`spot_summaries` gives it."""
    now = now or utcnow()
    runs = _served_runs(db, now)
    current, best = _current_and_best(db, runs, now, horizon_days, spot_id=spot.id)
    events = _active_events(db, now, spot_id=spot.id)
    research = research_out(db, spot.id)
    summary = _summary(
        spot,
        current,
        best,
        events,
        research.research_class if research else None,
        now,
        freshness=freshness_by_spot(db, runs, now).get(spot.id),
    )
    return SpotDetail(
        **summary.model_dump(),
        research=research,
        forecast_latitude=spot.forecast_latitude,
        forecast_longitude=spot.forecast_longitude,
        swell_window_min=spot.swell_window_min,
        swell_window_max=spot.swell_window_max,
        optimal_swell_direction_min=spot.optimal_swell_direction_min,
        optimal_swell_direction_max=spot.optimal_swell_direction_max,
        offshore_wind_direction=spot.offshore_wind_direction,
        min_swell_period_s=spot.min_swell_period_s,
        ideal_swell_period_s=spot.ideal_swell_period_s,
        tide_preference=spot.tide_preference.value if spot.tide_preference else None,
        height_factor=spot.height_factor,
        calibrated=bool(spot.calibration),
        seasonality=spot.seasonality,
        accessibility_notes=spot.accessibility_notes,
        hazards=spot.hazards,
        description=spot.description,
        physical_profile=_profile_out(spot),
        airports=[
            SpotAirportOut(
                airport=AirportOut.model_validate(a.airport),
                is_primary=a.is_primary,
                transfer_minutes=a.transfer_minutes,
                transfer_mode=a.transfer_mode,
                notes=a.notes,
            )
            for a in spot.airports
        ],
    )


def _profile_out(spot: SurfSpot) -> PhysicalProfileOut | None:
    row = spot.physical_profile
    if row is None:
        return None
    return PhysicalProfileOut(
        shore_normal_deg=row.shore_normal_deg,
        forecast_point_depth_m=row.forecast_point_depth_m,
        controlling_depth_m=row.controlling_depth_m,
        focusing_ref=row.focusing_ref,
        focusing_kappa=row.focusing_kappa,
        has_shadow_table=row.shadow_table is not None,
        data_quality=dict(row.data_quality or {}),
        notes=row.notes,
    )


def daily_summaries(
    points: list[ForecastPoint], tz: str, lat: float | None = None, lon: float | None = None
) -> list[DailySummary]:
    """One summary per spot-local day with daylight steps, as the spot timeline's days.

    Each step stands for its nearest-valid-step interval (``timesteps.centred_extents``,
    the last step keeping the spacing before it). With the spot's ``lat``/``lon``,
    ``good_hours`` is the time between first and last light of the day under Good or
    better steps (``sun.daylight_windows``), and the best time is a daylight step mostly
    between sunrise and sunset; without them the daylight steps' intervals count whole.
    So a 3-hourly step counts up to 3 hours, never 1, and never more than the day's light.
    """
    zone = ZoneInfo(tz)
    by_day: dict[date, list[ForecastPoint]] = defaultdict(list)
    for p in points:
        if p.is_daylight:
            by_day[p.valid_time.astimezone(zone).date()].append(p)
    times = [p.valid_time for p in points]
    extents = timesteps.centred_extents(times, timesteps.forward_hours(times))
    extent_of = {p.valid_time: e for p, e in zip(points, extents, strict=True)}
    good = min_score_for_label(QualityLabel.GOOD)
    located = lat is not None and lon is not None
    good_mask = [p.score >= good and (located or p.is_daylight) for p in points]
    out: list[DailySummary] = []
    for day in sorted(by_day):
        bounds = timesteps.local_day_bounds(day, tz)
        items = by_day[day]
        if lat is not None and lon is not None:
            around = (bounds[0] - timedelta(days=1), bounds[1] + timedelta(days=1))
            windows = daylight_windows(lat, lon, *around)
            sunlit = daylight_windows(lat, lon, *around, margin=timedelta(0))
            items = [p for p in items if timesteps.mostly_inside(extent_of[p.valid_time], sunlit)]
        else:
            windows = [bounds]
        if not items:
            continue
        top = max(items, key=lambda p: p.score)
        winds = [p.wind_speed_kmh for p in items if p.wind_speed_kmh is not None]
        out.append(
            DailySummary(
                date=day,
                best_score=top.score,
                best_label=label_for_score(top.score).value,
                best_time=top.valid_time,
                height_min_ft=min(p.breaking_height_min_ft for p in items),
                height_max_ft=max(p.breaking_height_max_ft for p in items),
                avg_wind_kmh=round(sum(winds) / len(winds), 1) if winds else None,
                good_hours=round(timesteps.covered_hours(extents, good_mask, windows, bounds), 2),
                confidence=round(sum(p.confidence for p in items) / len(items)),
                forecast_confidence_class=top.forecast_confidence_class,
                local_confidence_class=top.local_confidence_class,
            )
        )
    return out


def forecast_points(
    db: Session,
    run_id: int,
    spot_id: int,
    start: datetime,
    end: datetime,
    *,
    stale: bool = False,
) -> list[ForecastPoint]:
    rows = spot_hours(db, run_id, spot_id, start, end)
    return [
        ForecastPoint(
            valid_time=p.valid_time,
            is_daylight=p.is_daylight,
            sig_wave_height_m=f.sig_wave_height_m,
            wave_period_s=f.wave_period_s,
            wave_period_kind=f.wave_period_kind,
            wave_direction_deg=f.wave_direction_deg,
            primary_swell_height_m=f.primary_swell_height_m,
            primary_swell_period_s=f.primary_swell_period_s,
            primary_swell_direction_deg=f.primary_swell_direction_deg,
            secondary_swell_height_m=f.secondary_swell_height_m,
            secondary_swell_period_s=f.secondary_swell_period_s,
            secondary_swell_direction_deg=f.secondary_swell_direction_deg,
            tertiary_swell_height_m=f.tertiary_swell_height_m,
            tertiary_swell_period_s=f.tertiary_swell_period_s,
            tertiary_swell_direction_deg=f.tertiary_swell_direction_deg,
            wind_wave_height_m=f.wind_wave_height_m,
            wind_wave_period_s=f.wind_wave_period_s,
            wind_wave_direction_deg=f.wind_wave_direction_deg,
            wind_speed_kmh=f.wind_speed_kmh,
            wind_direction_deg=f.wind_direction_deg,
            wind_gust_kmh=f.wind_gust_kmh,
            pressure_msl_hpa=f.pressure_msl_hpa,
            sea_level_m=f.sea_level_m,
            wave_power_kw_m=f.wave_power_kw_m,
            breaking_height_min_ft=p.breaking_height_min_ft,
            breaking_height_max_ft=p.breaking_height_max_ft,
            breaking_height_mid_ft=p.breaking_height_mid_ft,
            breaking_p10_ft=p.breaking_p10_ft,
            breaking_p90_ft=p.breaking_p90_ft,
            uncertainty_kind=p.uncertainty_kind,
            breaking_height_method=p.breaking_height_method,
            algorithm_version=p.algorithm_version,
            ml_model_version=p.ml_model_version,
            score=p.score,
            label=p.label.value,
            confidence=p.confidence,
            confidence_label=p.confidence_label.value,
            wind_relation=p.wind_relation,
            explanation=p.explanation,
            components=p.components,
            **api_fields(p, stale=stale),
            **wind_fields(p.components, f.extra),
            **hour_outputs(p),
        )
        for p, f in rows
    ]


def _series(
    db: Session,
    source: ForecastSource,
    run: ForecastRun,
    role: str,
    spot_id: int,
    start: datetime,
    end: datetime,
) -> ModelSeries | None:
    # Only the columns the series shows (a whole forecast row is 2-3 times as large).
    w = WaveForecast
    rows = db.execute(
        select(
            w.valid_time,
            w.sig_wave_height_m,
            w.wave_period_s,
            w.wave_direction_deg,
            w.wind_speed_kmh,
            w.wind_direction_deg,
            w.wave_period_kind,
        )
        .where(
            w.run_id == run.id,
            w.spot_id == spot_id,
            w.valid_time >= start,
            w.valid_time <= end,
        )
        .order_by(w.valid_time)
    ).all()
    points = [
        ModelPoint(
            valid_time=r.valid_time,
            hs_m=r.sig_wave_height_m,
            tp_s=r.wave_period_s,
            direction_deg=r.wave_direction_deg,
            wind_kmh=r.wind_speed_kmh,
            wind_direction_deg=r.wind_direction_deg,
        )
        for r in rows
        if r.valid_time.hour % MODEL_SERIES_STEP_HOURS == 0
    ]
    if not points:
        return None
    return ModelSeries(
        source_code=source.code,
        name=source.name,
        family=source.family,
        kind=source.kind,
        role=role,
        is_demo=source.is_demo,
        run_issued_at=run.issued_at,
        period_kind=rows[0].wave_period_kind,
        points=points,
    )


def model_series(
    db: Session, spot: SurfSpot, run: ForecastRun, start: datetime, end: datetime
) -> list[ModelSeries]:
    """The authoritative run plus every other model's raw forecast, for comparison."""
    entries: list[tuple[ForecastSource, ForecastRun, str]] = [(run.source, run, "authoritative")]
    seen = {run.id}
    if run.source.kind == "blend":
        for key, role in (("members", "member"), ("cross_checks", "cross_check")):
            for m in run.details.get(key, []):
                member = db.get(ForecastRun, m.get("run_id"))
                if member is not None and member.id not in seen:
                    entries.append((member.source, member, role))
                    seen.add(member.id)
    for code in configured_source_codes():
        source = db.scalar(select(ForecastSource).where(ForecastSource.code == code))
        if source is None:
            continue
        latest = db.scalar(
            select(ForecastRun)
            .where(ForecastRun.source_id == source.id, ForecastRun.status.in_(USABLE))
            .order_by(ForecastRun.issued_at.desc(), ForecastRun.id.desc())
            .limit(1)
        )
        if latest is not None and latest.id not in seen:
            entries.append((source, latest, "reference"))
            seen.add(latest.id)
    out = [_series(db, src, r, role, spot.id, start, end) for src, r, role in entries]
    return [s for s in out if s is not None]


def ensemble_series(
    db: Session, spot: SurfSpot, start: datetime, end: datetime
) -> list[EnsemblePoint]:
    ens_run = db.scalar(
        select(ForecastRun)
        .join(ForecastSource, ForecastSource.id == ForecastRun.source_id)
        .where(ForecastSource.kind == "ensemble", ForecastRun.status.in_(USABLE))
        .order_by(ForecastRun.issued_at.desc(), ForecastRun.id.desc())
        .limit(1)
    )
    if ens_run is None:
        return []
    rows = db.scalars(
        select(ForecastEnsembleStat)
        .where(
            ForecastEnsembleStat.run_id == ens_run.id,
            ForecastEnsembleStat.spot_id == spot.id,
            ForecastEnsembleStat.valid_time >= start,
            ForecastEnsembleStat.valid_time <= end,
        )
        .order_by(ForecastEnsembleStat.valid_time)
    ).all()
    return [
        EnsemblePoint(
            valid_time=r.valid_time,
            hs_mean_m=r.hs_mean_m,
            hs_spread_m=r.hs_spread_m,
            members=r.members,
        )
        for r in rows
    ]


def blend_info(db: Session, run: ForecastRun, spot: SurfSpot) -> BlendInfo | None:
    if run.source.kind != "blend":
        return None
    flags: dict[str, int] = defaultdict(int)
    flagged = 0
    for (row_flags,) in db.execute(
        select(ForecastComparison.flags).where(
            ForecastComparison.blend_run_id == run.id, ForecastComparison.spot_id == spot.id
        )
    ):
        if row_flags:
            flagged += 1
        for f in row_flags:
            flags[f.split(":")[0]] += 1
    return BlendInfo(
        method=str(run.details.get("method", "")),
        members=list(run.details.get("members", [])),
        cross_checks=list(run.details.get("cross_checks", [])),
        flagged_hours=flagged,
        flags=dict(flags),
        skill=run.details.get("skill"),
    )


def source_attribution(db: Session, run: ForecastRun) -> str:
    """The attribution to show for a run. A blend repeats every member's attribution,
    because the members' licences (e.g. ECMWF's CC BY 4.0) require it wherever their data
    is shown, including in a combination."""
    if run.source.kind != "blend":
        return run.source.attribution
    codes = [m.get("code") for m in run.details.get("members", []) if m.get("code")]
    by_code = {
        s.code: s.attribution
        for s in db.scalars(select(ForecastSource).where(ForecastSource.code.in_(codes))).all()
    }
    parts = [run.source.attribution] + [by_code[c] for c in codes if by_code.get(c)]
    return " ".join(dict.fromkeys(parts))


# What a member shows of its source (its attribution and licence texts are most of a row).
_MEMBER_COLUMNS = (
    ForecastSource.code,
    ForecastSource.name,
    ForecastSource.family,
    ForecastSource.wave_model,
    ForecastSource.atmosphere_model,
)
MemberSource = Row[str, str, str, str | None, str | None]


def _member_out(
    source: ForecastSource | MemberSource, run_key: str, init_time: datetime | None
) -> RunMemberOut:
    """``source``: the source, or a row of its :data:`_MEMBER_COLUMNS`."""
    return RunMemberOut(
        code=source.code,
        name=source.name,
        family=source.family,
        run_key=run_key,
        init_time=init_time,
        wave_model=source.wave_model,
        atmosphere_model=source.atmosphere_model,
    )


def _served_runs(db: Session, now: datetime) -> dict[int, ForecastRun]:
    """``queries.served_runs_by_spot`` at ``now`` (read once inside :func:`shared_hour_reads`)."""
    return _shared(db, ("runs", now), lambda: served_runs_by_spot(db, now))


def authoritative_run(db: Session, spot_id: int, now: datetime) -> ForecastRun | None:
    """The run served for the spot (``queries.served_runs_by_spot``: the published version's
    run, else the legacy selection; read once per ``now`` inside :func:`shared_hour_reads`)."""
    return _served_runs(db, now).get(spot_id)


def served_freshness(db: Session, spot_id: int, now: datetime) -> SpotFreshnessOut | None:
    """The spot's freshness at ``now`` (``serving.freshness_by_spot``; read once per ``now``
    inside :func:`shared_hour_reads`); None for a spot nothing is served for."""
    return _shared(
        db, ("freshness", now), lambda: freshness_by_spot(db, _served_runs(db, now), now)
    ).get(spot_id)


def forecast_source_out(db: Session, run: ForecastRun) -> ForecastSourceOut:
    """The run's source with its attribution (built once per run inside
    :func:`shared_hour_reads`)."""

    def build() -> ForecastSourceOut:
        source = run.source
        return ForecastSourceOut(
            code=source.code,
            name=source.name,
            is_demo=source.is_demo,
            attribution=source_attribution(db, run),
            wave_model=source.wave_model,
            atmosphere_model=source.atmosphere_model,
            attribution_links=source_attribution_links(db, run),
        )

    return _shared(db, ("source", run.id), build)


def run_members(db: Session, run: ForecastRun) -> list[RunMemberOut]:
    """The model runs behind ``run`` with their own init times: a blend's members (from its
    details; the blend's ``issued_at`` is only the oldest member's), otherwise the run
    itself (built once per run inside :func:`shared_hour_reads`)."""
    return _shared(db, ("members", run.id), lambda: _run_members(db, run))


def _run_members(db: Session, run: ForecastRun) -> list[RunMemberOut]:
    if run.source.kind != "blend":
        return [_member_out(run.source, run.run_key, run.issued_at)]
    members = [m for m in (run.details or {}).get("members", []) if m.get("code")]
    codes = [str(m["code"]) for m in members]
    sources = {
        s.code: s
        for s in db.execute(select(*_MEMBER_COLUMNS).where(ForecastSource.code.in_(codes))).all()
    }
    out: list[RunMemberOut] = []
    for m in members:
        source = sources.get(str(m["code"]))
        if source is None:
            continue
        issued = m.get("issued_at")
        out.append(
            _member_out(
                source,
                str(m.get("run_key") or ""),
                datetime.fromisoformat(str(issued)) if issued else None,
            )
        )
    return out


def spot_forecast(
    db: Session, spot: SurfSpot, days: int = 10, now: datetime | None = None
) -> SpotForecastOut:
    now = now or utcnow()
    run = authoritative_run(db, spot.id, now)
    freshness = served_freshness(db, spot.id, now)
    if run is None:
        return SpotForecastOut(
            spot_slug=spot.slug,
            timezone=spot.timezone,
            available=False,
            message="No forecast has been ingested for this spot yet.",
            freshness=freshness,
        )
    start = now - timedelta(hours=3)
    end = now + timedelta(days=days)
    stale = is_stale(run, now)
    points = forecast_points(db, run.id, spot.id, start, end, stale=stale)
    # Report the method the stored predictions actually used.
    method = points[0].breaking_height_method if points else None
    return SpotForecastOut(
        spot_slug=spot.slug,
        timezone=spot.timezone,
        available=bool(points),
        message=None if points else "The latest model run has no future data for this spot.",
        source=forecast_source_out(db, run),
        run=ForecastRunOut(
            run_key=run.run_key,
            issued_at=run.issued_at,
            completed_at=run.completed_at,
            wave_model_run_at=run.wave_model_run_at,
            atmosphere_model_run_at=run.atmosphere_model_run_at,
            status=run.status.value,
            members=run_members(db, run),
        ),
        stale=stale,
        # The alert job's own rule (notifications.alerts): alerts are held back only from
        # STALE_ALERT_FACTOR times the stale-after age, not as soon as the run is stale.
        alerts_paused=is_stale(run, now, factor=STALE_ALERT_FACTOR),
        # generated_at stays None here: the rest of this response depends only on the stored
        # data, and a per-request clock value would make identical forecasts compare
        # unequal (the spot timeline carries its own generated_at).
        breaking_height_method=method,
        algorithm_version=points[0].algorithm_version if points else None,
        calibrated=bool(spot.calibration),
        points=points,
        daily=daily_summaries(points, spot.timezone, spot.latitude, spot.longitude),
        models=model_series(db, spot, run, start, end),
        ensemble=ensemble_series(db, spot, start, end),
        blend=blend_info(db, run, spot),
        freshness=freshness,
    )


def upcoming_events(
    db: Session, now: datetime | None = None, limit: int = 20, min_score: int = 0
) -> list[EventOut]:
    now = now or utcnow()
    rows = db.scalars(
        select(SwellEvent)
        .where(
            SwellEvent.status == SwellEventStatus.ACTIVE,
            SwellEvent.end_time >= now,
            SwellEvent.peak_score >= min_score,
        )
        .order_by(SwellEvent.peak_score.desc(), SwellEvent.start_time)
        .limit(limit)
    ).all()
    links: AttributionMemo = {}
    return [event_out(e, now, links=links) for e in rows]


def spot_events(db: Session, spot: SurfSpot, now: datetime | None = None) -> list[EventOut]:
    """The spot's active and downgraded events that have not ended, by start time."""
    now = now or utcnow()
    rows = db.scalars(
        select(SwellEvent)
        .where(
            SwellEvent.spot_id == spot.id,
            SwellEvent.end_time >= now,
            SwellEvent.status.in_((SwellEventStatus.ACTIVE, SwellEventStatus.DOWNGRADED)),
        )
        .order_by(SwellEvent.start_time)
    ).all()
    links: AttributionMemo = {}
    return [event_out(e, now, links=links) for e in rows]


def event_counts(db: Session) -> dict[str, int]:
    rows = db.execute(select(SwellEvent.status, func.count()).group_by(SwellEvent.status)).all()
    return {str(status.value): int(n) for status, n in rows}


MAP_STEP_HOURS = 3


def map_snapshot(db: Session, at: datetime | None, now: datetime | None = None) -> MapSnapshotOut:
    """Conditions at every active spot for one forecast hour (nearest stored timestep within
    ±90 min), read from the latest predictions already in the database. Never fetches model
    data, so scrubbing the map's time control is cheap."""
    now = now or utcnow()
    spots = db.scalars(select(SurfSpot).where(SurfSpot.is_active).order_by(SurfSpot.name)).all()
    runs = served_runs_by_spot(db, now)
    freshness = freshness_by_spot(db, runs, now)
    version = served_version(db)
    run_ids = {r.id for r in runs.values()}
    range_start = range_end = None
    if run_ids:
        lo, hi = db.execute(
            select(
                func.min(SurfQualityPrediction.valid_time),
                func.max(SurfQualityPrediction.valid_time),
            ).where(SurfQualityPrediction.run_id.in_(run_ids))
        ).one()
        floor = (now - timedelta(hours=MAP_STEP_HOURS)).replace(minute=0, second=0, microsecond=0)
        range_start = max(lo, floor).replace(minute=0, second=0, microsecond=0) if lo else None
        range_end = hi
    target = at or now
    if range_start and range_end:
        target = min(max(target, range_start), range_end)
    target = target.replace(minute=0, second=0, microsecond=0)

    conditions: dict[int, Conditions] = {}
    stale = {r.id: is_stale(r, now) for r in runs.values()}
    if run_ids:
        # The hours within the window, by their time only, from each spot's own run; then
        # the nearest one per spot in full (two statements; see _current_and_best). On a tie
        # the earlier hour wins.
        window = timedelta(minutes=90)
        p = SurfQualityPrediction
        scan = (
            select(p.id, p.spot_id, p.valid_time)
            .where(
                tuple_(p.spot_id, p.run_id).in_([(sid, r.id) for sid, r in runs.items()]),
                p.valid_time >= target - window,
                p.valid_time <= target + window,
            )
            .order_by(p.spot_id, p.valid_time)
        )
        nearest: dict[int, tuple[float, int]] = {}
        for row in db.execute(scan):
            gap = abs((row.valid_time - target).total_seconds())
            if gap < nearest.get(row.spot_id, (float("inf"), 0))[0]:
                nearest[row.spot_id] = (gap, row.id)
        full = _predictions_by_id(db, {i for _, i in nearest.values()})
        for sid, (_, prediction_id) in nearest.items():
            if prediction_id in full:  # absent if pruned between the two statements
                run = runs[sid]
                conditions[sid] = _conditions(*full[prediction_id], run, stale=stale[run.id])

    covering: dict[int, int] = defaultdict(int)
    upcoming: dict[int, int] = defaultdict(int)
    for spot_id, start_time, end_time in db.execute(
        select(SwellEvent.spot_id, SwellEvent.start_time, SwellEvent.end_time).where(
            SwellEvent.status == SwellEventStatus.ACTIVE, SwellEvent.end_time >= now
        )
    ):
        upcoming[spot_id] += 1
        if start_time <= target <= end_time:
            covering[spot_id] += 1
    # Freshness of the newest authoritative run behind the snapshot.
    newest = max(runs.values(), key=lambda r: (r.issued_at, r.id), default=None)
    links: dict[tuple[str, str], AttributionLinkOut] = {}
    for run in sorted({r.id: r for r in runs.values()}.values(), key=lambda r: r.id):
        for link in source_attribution_links(db, run):
            links.setdefault((link.label, link.href), link)
    return MapSnapshotOut(
        version_id=version.id if version is not None else None,
        attribution_links=list(links.values()),
        stale=newest is not None and is_stale(newest, now),
        newest_run_issued_at=newest.issued_at if newest is not None else None,
        last_updated_at=newest.completed_at if newest is not None else None,
        requested_at=at,
        valid_time=target if run_ids else None,
        range_start=range_start,
        range_end=range_end,
        step_hours=MAP_STEP_HOURS,
        spots=[
            MapSpotOut(
                slug=s.slug,
                conditions=conditions.get(s.id),
                event_at_time=covering.get(s.id, 0) > 0,
                upcoming_event_count=upcoming.get(s.id, 0),
                freshness=freshness.get(s.id),
            )
            for s in spots
        ],
    )
