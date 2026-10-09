"""Read models for spots, forecasts and events (used by the API)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.models import ForecastRun, SurfQualityPrediction, SurfSpot, SwellEvent, WaveForecast
from app.models.enums import SwellEventStatus
from app.schemas.spots import (
    AirportOut,
    Conditions,
    DailySummary,
    EventOut,
    ForecastPoint,
    ForecastRunOut,
    ForecastSourceOut,
    SpotAirportOut,
    SpotDetail,
    SpotForecastOut,
    SpotMini,
    SpotSummary,
)
from app.services.forecasts.queries import is_stale, latest_runs_by_spot
from app.services.surf_quality.breaking import METHOD_CALIBRATED
from app.services.surf_quality.scoring import label_for_score


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
    )


def _conditions(p: SurfQualityPrediction, f: WaveForecast) -> Conditions:
    return Conditions(
        valid_time=p.valid_time,
        score=p.score,
        label=p.label.value,
        breaking_height_min_ft=p.breaking_height_min_ft,
        breaking_height_max_ft=p.breaking_height_max_ft,
        swell_height_m=f.primary_swell_height_m
        if f.primary_swell_height_m is not None
        else f.sig_wave_height_m,
        swell_period_s=f.primary_swell_period_s
        if f.primary_swell_period_s is not None
        else f.wave_period_s,
        swell_direction_deg=(
            f.primary_swell_direction_deg
            if f.primary_swell_direction_deg is not None
            else f.wave_direction_deg
        ),
        wind_speed_kmh=f.wind_speed_kmh,
        wind_direction_deg=f.wind_direction_deg,
        wind_relation=p.wind_relation,
        confidence=p.confidence,
        confidence_label=p.confidence_label.value,
        is_daylight=p.is_daylight,
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
            if p.spot_id not in current and p.valid_time >= now - timedelta(minutes=30):
                current[p.spot_id] = _conditions(p, f)
            if p.is_daylight and (p.spot_id not in best or p.score > best[p.spot_id].score):
                best[p.spot_id] = _conditions(p, f)

    events: dict[int, list[SwellEvent]] = defaultdict(list)
    for e in db.scalars(
        select(SwellEvent)
        .where(SwellEvent.status == SwellEventStatus.ACTIVE, SwellEvent.end_time >= now)
        .order_by(SwellEvent.start_time)
    ).all():
        events[e.spot_id].append(e)

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
            )
        )
    return out


def spot_detail(db: Session, spot: SurfSpot, now: datetime | None = None) -> SpotDetail:
    summary = next((s for s in spot_summaries(db, now) if s.id == spot.id), None)
    base = summary.model_dump() if summary else _summary_fields(spot)
    return SpotDetail(
        **base,
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
            )
        )
    return out


def forecast_points(
    db: Session, run_id: int, spot_id: int, start: datetime, end: datetime
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
            primary_swell_height_m=f.primary_swell_height_m,
            primary_swell_period_s=f.primary_swell_period_s,
            primary_swell_direction_deg=f.primary_swell_direction_deg,
            secondary_swell_height_m=f.secondary_swell_height_m,
            secondary_swell_period_s=f.secondary_swell_period_s,
            secondary_swell_direction_deg=f.secondary_swell_direction_deg,
            wind_wave_height_m=f.wind_wave_height_m,
            wind_wave_period_s=f.wind_wave_period_s,
            wind_speed_kmh=f.wind_speed_kmh,
            wind_direction_deg=f.wind_direction_deg,
            wind_gust_kmh=f.wind_gust_kmh,
            pressure_msl_hpa=f.pressure_msl_hpa,
            sea_level_m=f.sea_level_m,
            wave_power_kw_m=f.wave_power_kw_m,
            breaking_height_min_ft=p.breaking_height_min_ft,
            breaking_height_max_ft=p.breaking_height_max_ft,
            score=p.score,
            label=p.label.value,
            confidence=p.confidence,
            confidence_label=p.confidence_label.value,
            wind_relation=p.wind_relation,
            explanation=p.explanation,
            components=p.components,
        )
        for p, f in rows
    ]


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
    points = forecast_points(db, run.id, spot.id, start, now + timedelta(days=days))
    source = run.source
    method = None
    if points:
        method = METHOD_CALIBRATED if spot.calibration else "komar_gaughan_v1_uncalibrated"
    return SpotForecastOut(
        spot_slug=spot.slug,
        timezone=spot.timezone,
        available=bool(points),
        message=None if points else "The latest model run has no future data for this spot.",
        source=ForecastSourceOut(
            code=source.code,
            name=source.name,
            is_demo=source.is_demo,
            attribution=source.attribution,
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
        stale=is_stale(run, now),
        breaking_height_method=method,
        calibrated=bool(spot.calibration),
        points=points,
        daily=daily_summaries(points, spot.timezone),
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
