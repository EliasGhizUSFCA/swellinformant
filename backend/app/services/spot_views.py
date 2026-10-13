"""Read models for spots, forecasts and events (used by the API)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, object_session

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
from app.models.enums import SwellEventStatus
from app.schemas.spots import (
    AirportOut,
    BlendInfo,
    Conditions,
    DailySummary,
    EnsemblePoint,
    EventOut,
    ForecastPoint,
    ForecastRunOut,
    ForecastSourceOut,
    MapSnapshotOut,
    MapSpotOut,
    ModelPoint,
    ModelSeries,
    PhysicalProfileOut,
    SpotAirportOut,
    SpotDetail,
    SpotForecastOut,
    SpotMini,
    SpotSummary,
)
from app.services.forecasts.queries import USABLE, is_stale, latest_runs_by_spot, run_id_is_stale
from app.services.forecasts.registry import configured_source_codes
from app.services.research.views import research_classes, research_out
from app.services.surf_quality.breaking import dominant_component
from app.services.surf_quality.forecast_confidence import api_fields
from app.services.surf_quality.scoring import label_for_score
from app.services.swell_detection.service import peak_confidence

MODEL_SERIES_STEP_HOURS = 3


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


def event_out(
    event: SwellEvent, now: datetime | None = None, include_history: bool = False
) -> EventOut:
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
        **peak_confidence(event, stale=stale),
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


def spot_summaries(
    db: Session, now: datetime | None = None, horizon_days: int = 7
) -> list[SpotSummary]:
    """All active spots with current conditions, best upcoming window and next event."""
    now = now or utcnow()
    spots = db.scalars(select(SurfSpot).where(SurfSpot.is_active).order_by(SurfSpot.name)).all()
    runs = latest_runs_by_spot(db, now)
    run_ids = {r.id for r in runs.values()}
    stale = {r.id: is_stale(r, now) for r in runs.values()}

    current: dict[int, Conditions] = {}
    best: dict[int, Conditions] = {}
    if run_ids:
        rows = db.execute(
            select(SurfQualityPrediction, WaveForecast)
            .join(WaveForecast, WaveForecast.id == SurfQualityPrediction.forecast_id)
            .where(
                SurfQualityPrediction.run_id.in_(run_ids),
                SurfQualityPrediction.valid_time >= now - timedelta(hours=1),
                SurfQualityPrediction.valid_time <= now + timedelta(days=horizon_days),
            )
            .order_by(SurfQualityPrediction.valid_time)
        ).all()
        for p, f in rows:
            if runs.get(p.spot_id) is None or runs[p.spot_id].id != p.run_id:
                continue
            run = runs[p.spot_id]
            if p.spot_id not in current and p.valid_time >= now - timedelta(minutes=30):
                current[p.spot_id] = _conditions(p, f, run, stale=stale[run.id])
            if p.is_daylight and (p.spot_id not in best or p.score > best[p.spot_id].score):
                best[p.spot_id] = _conditions(p, f, run, stale=stale[run.id])

    events: dict[int, list[SwellEvent]] = defaultdict(list)
    for e in db.scalars(
        select(SwellEvent)
        .where(SwellEvent.status == SwellEventStatus.ACTIVE, SwellEvent.end_time >= now)
        .order_by(SwellEvent.start_time)
    ).all():
        events[e.spot_id].append(e)

    research = research_classes(db)  # one query for all spots
    out: list[SpotSummary] = []
    for s in spots:
        upcoming = events.get(s.id, [])
        out.append(
            SpotSummary(
                **_summary_fields(s),  # type: ignore[arg-type]
                current=current.get(s.id),
                best_upcoming=best.get(s.id),
                next_event=event_out(upcoming[0], now) if upcoming else None,
                upcoming_event_count=len(upcoming),
                research_class=research.get(s.id),
            )
        )
    return out


def spot_detail(db: Session, spot: SurfSpot, now: datetime | None = None) -> SpotDetail:
    summary = next((s for s in spot_summaries(db, now) if s.id == spot.id), None)
    base = summary.model_dump() if summary else _summary_fields(spot)
    research = research_out(db, spot.id)
    base["research_class"] = research.research_class if research else None
    return SpotDetail(
        **base,
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


def daily_summaries(points: list[ForecastPoint], tz: str) -> list[DailySummary]:
    zone = ZoneInfo(tz)
    by_day: dict[object, list[ForecastPoint]] = defaultdict(list)
    for p in points:
        if p.is_daylight:
            by_day[p.valid_time.astimezone(zone).date()].append(p)
    out: list[DailySummary] = []
    for day in sorted(by_day):  # type: ignore[type-var]
        items = by_day[day]
        top = max(items, key=lambda p: p.score)
        winds = [p.wind_speed_kmh for p in items if p.wind_speed_kmh is not None]
        out.append(
            DailySummary(
                date=day,  # type: ignore[arg-type]
                best_score=top.score,
                best_label=label_for_score(top.score).value,
                best_time=top.valid_time,
                height_min_ft=min(p.breaking_height_min_ft for p in items),
                height_max_ft=max(p.breaking_height_max_ft for p in items),
                avg_wind_kmh=round(sum(winds) / len(winds), 1) if winds else None,
                good_hours=sum(1 for p in items if p.score >= 57),
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
    rows = db.execute(
        select(SurfQualityPrediction, WaveForecast)
        .join(WaveForecast, WaveForecast.id == SurfQualityPrediction.forecast_id)
        .where(
            SurfQualityPrediction.run_id == run_id,
            SurfQualityPrediction.spot_id == spot_id,
            SurfQualityPrediction.valid_time >= start,
            SurfQualityPrediction.valid_time <= end,
        )
        .order_by(SurfQualityPrediction.valid_time)
    ).all()
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
    rows = db.scalars(
        select(WaveForecast)
        .where(
            WaveForecast.run_id == run.id,
            WaveForecast.spot_id == spot_id,
            WaveForecast.valid_time >= start,
            WaveForecast.valid_time <= end,
        )
        .order_by(WaveForecast.valid_time)
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


def spot_forecast(
    db: Session, spot: SurfSpot, days: int = 10, now: datetime | None = None
) -> SpotForecastOut:
    now = now or utcnow()
    run: ForecastRun | None = latest_runs_by_spot(db, now).get(spot.id)
    if run is None:
        return SpotForecastOut(
            spot_slug=spot.slug,
            timezone=spot.timezone,
            available=False,
            message="No forecast has been ingested for this spot yet.",
        )
    start = now - timedelta(hours=3)
    end = now + timedelta(days=days)
    stale = is_stale(run, now)
    points = forecast_points(db, run.id, spot.id, start, end, stale=stale)
    source = run.source
    # Report the method the stored predictions actually used.
    method = points[0].breaking_height_method if points else None
    return SpotForecastOut(
        spot_slug=spot.slug,
        timezone=spot.timezone,
        available=bool(points),
        message=None if points else "The latest model run has no future data for this spot.",
        source=ForecastSourceOut(
            code=source.code,
            name=source.name,
            is_demo=source.is_demo,
            attribution=source_attribution(db, run),
            wave_model=source.wave_model,
            atmosphere_model=source.atmosphere_model,
        ),
        run=ForecastRunOut(
            run_key=run.run_key,
            issued_at=run.issued_at,
            completed_at=run.completed_at,
            wave_model_run_at=run.wave_model_run_at,
            atmosphere_model_run_at=run.atmosphere_model_run_at,
            status=run.status.value,
        ),
        stale=stale,
        breaking_height_method=method,
        algorithm_version=points[0].algorithm_version if points else None,
        calibrated=bool(spot.calibration),
        points=points,
        daily=daily_summaries(points, spot.timezone),
        models=model_series(db, spot, run, start, end),
        ensemble=ensemble_series(db, spot, start, end),
        blend=blend_info(db, run, spot),
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
    return [event_out(e, now) for e in rows]


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
    runs = latest_runs_by_spot(db, now)
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
        window = timedelta(minutes=90)
        rows = db.execute(
            select(SurfQualityPrediction, WaveForecast)
            .join(WaveForecast, WaveForecast.id == SurfQualityPrediction.forecast_id)
            .where(
                SurfQualityPrediction.run_id.in_(run_ids),
                SurfQualityPrediction.valid_time >= target - window,
                SurfQualityPrediction.valid_time <= target + window,
            )
        ).all()
        best_gap: dict[int, float] = {}
        for p, f in rows:
            run = runs.get(p.spot_id)
            if run is None or run.id != p.run_id:
                continue
            gap = abs((p.valid_time - target).total_seconds())
            if gap < best_gap.get(p.spot_id, float("inf")):
                best_gap[p.spot_id] = gap
                conditions[p.spot_id] = _conditions(p, f, run, stale=stale[run.id])

    covering: dict[int, int] = defaultdict(int)
    upcoming: dict[int, int] = defaultdict(int)
    for e in db.scalars(
        select(SwellEvent).where(
            SwellEvent.status == SwellEventStatus.ACTIVE, SwellEvent.end_time >= now
        )
    ).all():
        upcoming[e.spot_id] += 1
        if e.start_time <= target <= e.end_time:
            covering[e.spot_id] += 1
    return MapSnapshotOut(
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
            )
            for s in spots
        ],
    )
