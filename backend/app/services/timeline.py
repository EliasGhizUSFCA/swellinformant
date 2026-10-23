"""The spot forecast timeline (contract: app/schemas/timeline.py).

Everything the spot page's synchronised chart rows draw, read from stored data only:

* the authoritative run is the one ``/forecast`` uses (``latest_runs_by_spot``), over the
  same window (from 3 hours ago to ``days`` ahead), so every step's score and label equal
  the forecast's for the same valid time;
* steps are the stored valid times at the model's native spacing; the timeline adds
  nothing between them. ``step_hours`` is the gap to the next valid time, and under the
  nearest-valid-step rule a step stands for the interval halfway back to the previous valid
  time and halfway on to the next (``timesteps.centred_extents``, as the charts draw it). A
  blend step may hold a member's value interpolated in time between that member's own
  steps (or a value its source interpolated): ``interpolated`` and
  ``interpolated_members`` say so, and ``steps_meta.interpolation`` is then "some_steps";
* local days come from ``timesteps.local_day_bounds`` (23 or 25 h when the clocks change)
  and their sunrise and sunset from ``surf_quality.sun`` (the same approximation that
  decides each step's ``is_daylight``), widened by ``TWILIGHT_MARGIN`` for first and last
  light. A day's ``good_hours`` is the time between first and last light covered by the
  intervals of Good-or-better steps, and its best time is chosen among daylight steps
  whose interval lies mostly between sunrise and sunset;
* partitions are listed only when the model stored them; the one that drives the surf is
  the partition with the largest predicted breaking height (``breaking.dominant_component``);
* wind strength and favourability come from the stored ``wind_detail`` (algorithm 2.1.0);
  rows scored before it carry the wind fit and relation only, and the rest stays None;
* the tide and observation blocks are app.services.tides.api's; a step's tide height is
  read from that block's curve, never from model sea level.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_left
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import ForecastRun, ForecastSource, SurfQualityPrediction, SurfSpot, WaveForecast
from app.models.enums import QualityLabel
from app.schemas.spots import ForecastSourceOut, RunMemberOut
from app.schemas.tides import ObservationsBlockOut, TideBlockOut
from app.schemas.timeline import (
    GustCause,
    PartitionKind,
    PartitionOut,
    ProvenanceOut,
    SpotTimelineOut,
    StepBreakingOut,
    StepOffshoreOut,
    StepQualityOut,
    StepSegmentOut,
    StepsMetaOut,
    StepTideOut,
    StepWhyOut,
    StepWindOut,
    TimelineDayOut,
    TimelineRunOut,
    TimelineStepOut,
    TimelineUnitsOut,
    WindDisagreement,
)
from app.services import timesteps
from app.services.forecasts.base import EXTRA_GUST_KIND, EXTRA_INTERPOLATED
from app.services.forecasts.queries import (
    BLEND_CODE,
    is_stale,
    latest_runs_by_spot,
    source_attribution_links,
    stale_after_hours,
)
from app.services.spot_views import source_attribution, spot_hours
from app.services.surf_quality.forecast_confidence import STALE_REASON, api_fields
from app.services.surf_quality.scoring import min_score_for_label
from app.services.surf_quality.sun import TWILIGHT_MARGIN, daylight_windows, sun_times
from app.services.tides.api import spot_observations_block, spot_tide_block
from app.services.tides.stage import TideStage, tide_stages_for

# Same window as /forecast (spot_views.spot_forecast): the current step is kept a while.
LOOKBACK = timedelta(hours=3)
NO_FORECAST = "No forecast has been ingested for this spot yet."
NO_FUTURE_DATA = "The latest model run has no future data for this spot."

# Stored factor fits shown in "Why this forecast?" (0..1, present only with their inputs).
FIT_KEYS = ("swell_direction", "swell_period", "wave_size", "wind", "tide", "consistency")
# Stored gates (0..1 multipliers on the score), by the short name the contract uses.
GATE_KEYS = {"size": "size_gate", "period": "period_gate", "wind": "wind_gate"}

# Wave-system columns of wave_forecasts, in the order the timeline lists them.
_PARTITION_COLUMNS: tuple[tuple[PartitionKind, str], ...] = (
    ("primary_swell", "primary_swell"),
    ("secondary_swell", "secondary_swell"),
    ("tertiary_swell", "tertiary_swell"),
    ("wind_sea", "wind_wave"),
)

# A tide curve point counts for a step only if it lies within this distance of the step's
# time (curves are 30-60 minute spaced, so a gap this wide means the curve is missing).
TIDE_MAX_GAP = timedelta(minutes=90)


def _hours(td: timedelta) -> float:
    return td.total_seconds() / 3600.0


def _r(value: float | None, digits: int) -> float | None:
    """Model values to the precision they carry (mm, 0.01 s, 0.1 deg, 0.01 km/h): the
    stored floats have spurious digits from unit conversion and blending."""
    return None if value is None else round(value, digits)


def driving_partition(pred: SurfQualityPrediction) -> str | None:
    """Name of the partition that drives the surf: the largest predicted breaking height in
    the stored nearshore breakdown, as ``breaking.dominant_component`` picks it. None
    without a breakdown (only bulk parameters were available)."""
    breakdown = (pred.components or {}).get("breaking") or {}
    parts = [p for p in breakdown.get("partitions") or [] if (p.get("breaking_m") or 0) > 0]
    if not parts:
        return None
    name = max(parts, key=lambda p: p["breaking_m"]).get("name")
    return str(name) if name else None


def _partitions(pred: SurfQualityPrediction, fc: WaveForecast) -> list[PartitionOut]:
    breakdown = (pred.components or {}).get("breaking") or {}
    breaking_ft = {
        str(p.get("name")): p.get("breaking_ft") for p in breakdown.get("partitions") or []
    }
    driver = driving_partition(pred)
    out: list[PartitionOut] = []
    for kind, column in _PARTITION_COLUMNS:
        height = getattr(fc, f"{column}_height_m")
        # An absent system is None (never 0); a zero height is no wave system either.
        if height is None or height <= 0:
            continue
        ft = breaking_ft.get(kind)
        out.append(
            PartitionOut(
                kind=kind,
                height_m=round(height, 3),
                period_s=_r(getattr(fc, f"{column}_period_s"), 2),
                dir_from_deg=_r(getattr(fc, f"{column}_direction_deg"), 1),
                breaking_ft=float(ft) if ft is not None else None,
                drives_surf=kind == driver,
            )
        )
    return out


# The blend's wind flags (blend.compare_hour), as the step's ``models_disagree`` names them.
_WIND_DISAGREEMENT: tuple[tuple[str, WindDisagreement], ...] = (
    ("wind_direction_disagree", "direction"),
    ("wind_disagree", "speed"),
)


def _gust_cause(fc: WaveForecast) -> GustCause | None:
    """Why the gust shown is below the mean wind shown, or None when it is not: "source"
    when the gust's own model(s) put it below their own mean wind, "blend" when the gust
    comes from fewer models than the blended mean and is not below their own mean."""
    speed, gust = fc.wind_speed_kmh, fc.wind_gust_kmh
    if gust is None or speed is None or gust >= speed:
        return None
    member_mean = (fc.extra or {}).get("gust_member_mean_kmh")
    if isinstance(member_mean, int | float) and gust >= member_mean:
        return "blend"
    return "source"


def _wind(pred: SurfQualityPrediction, fc: WaveForecast) -> StepWindOut | None:
    if fc.wind_speed_kmh is None and fc.wind_direction_deg is None:
        return None
    components = pred.components or {}
    detail = components.get("wind_detail") or {}
    fit = detail.get("q", components.get("wind"))
    speed, gust = fc.wind_speed_kmh, fc.wind_gust_kmh
    gust_kind = (fc.extra or {}).get(EXTRA_GUST_KIND)
    flags = ((pred.confidence_inputs or {}).get("disagreement") or {}).get("flags") or []
    cause = _gust_cause(fc)
    return StepWindOut(
        speed_kmh=_r(speed, 2),
        gust_kmh=_r(gust, 2),
        gust_kind=str(gust_kind) if gust_kind else None,
        gust_below_mean=cause is not None,
        gust_below_mean_cause=cause,
        models_disagree=[name for flag, name in _WIND_DISAGREEMENT if flag in flags],
        dir_from_deg=_r(fc.wind_direction_deg, 1),
        relation=pred.wind_relation,
        strength=detail.get("strength"),
        favourability=detail.get("favourability"),
        fit=float(fit) if fit is not None else None,
        offshore_component_kmh=detail.get("v_offshore_kmh"),
        cross_component_kmh=detail.get("v_cross_kmh"),
    )


def _why(pred: SurfQualityPrediction) -> StepWhyOut:
    components = pred.components or {}
    fits = {k: float(components[k]) for k in FIT_KEYS if components.get(k) is not None}
    gates = {
        name: float(components[key])
        for name, key in GATE_KEYS.items()
        if components.get(key) is not None
    }
    contributions = components.get("contributions")
    return StepWhyOut(
        fits=fits,
        gates=gates,
        contributions=(
            {str(k): float(v) for k, v in contributions.items()}
            if isinstance(contributions, dict)
            else None
        ),
        tide_included=components.get("tide") is not None or "tide_detail" in components,
        explanation=pred.explanation,
    )


def _quality(pred: SurfQualityPrediction, stale: bool) -> StepQualityOut:
    fields = api_fields(pred, stale=stale)
    reasons = fields.get("forecast_confidence_reasons")
    if reasons is None:
        # Forecast rules only: the combined list also carries the disputed-wind note and
        # the research sentence, which are not about the offshore forecast.
        reasons = [str(r) for r in (pred.confidence_inputs or {}).get("reasons") or []]
        if STALE_REASON in fields.get("confidence_reasons", []) and STALE_REASON not in reasons:
            reasons = [STALE_REASON, *reasons]
    return StepQualityOut(
        score=pred.score,
        label=pred.label.value,
        forecast_confidence_class=fields["forecast_confidence_class"],
        local_confidence_class=fields["local_confidence_class"],
        forecast_confidence_reasons=list(reasons),
        local_confidence_reason=fields["local_confidence_reason"],
    )


def _tide_at(t: datetime, tide: TideBlockOut, stage: TideStage | None) -> StepTideOut | None:
    """The tide at ``t`` from the block's curve (linear between its neighbouring points,
    which are at most an hour apart); rising/falling from the next high or low water."""
    if tide.status == "unavailable" or not tide.curve:
        return None
    times = [p.t for p in tide.curve]
    i = bisect_left(times, t)
    if i < len(times) and times[i] == t:
        height = tide.curve[i].height_m
    elif 0 < i < len(times):
        a, b = tide.curve[i - 1], tide.curve[i]
        if b.t - a.t > 2 * TIDE_MAX_GAP:
            return None
        frac = (t - a.t) / (b.t - a.t)
        height = a.height_m + (b.height_m - a.height_m) * frac
    else:
        return None
    rising: bool | None = None
    following = next((e for e in tide.events if e.t > t), None)
    if following is not None:
        rising = following.kind == "high"
    elif stage is not None:
        rising = stage.rising
    return StepTideOut(
        height_m=round(height, 3),
        rising=rising,
        stage=round(stage.stage, 3) if stage is not None else None,
    )


def _sun_events(
    lat: float, lon: float, start: datetime, end: datetime, day: date
) -> tuple[datetime | None, datetime | None, str]:
    """Sunrise and sunset falling inside the local day [start, end) and the day's polar
    state. ``sun_times`` works on the local *solar* date, which can differ from the civil
    date by a day near the date line, so the neighbouring solar dates are checked too."""
    rises: list[datetime] = []
    sets: list[datetime] = []
    state = "normal"
    for offset in (-1, 0, 1):
        sunrise, sunset, solar_state = sun_times(lat, lon, day + timedelta(days=offset))
        if offset == 0:
            state = solar_state
        if sunrise is not None and start <= sunrise < end:
            rises.append(sunrise)
        if sunset is not None and start <= sunset < end:
            sets.append(sunset)
    return (rises[0] if rises else None), (sets[-1] if sets else None), state


# A step can be a day's best time only when at least this share of its interval is between
# sunrise and sunset (no twilight margin: a 3-hour step centred 20 minutes after sunset is
# mostly dark and is never recommended).
BEST_MIN_LIGHT_SHARE = 0.5


def _days(
    spot: SurfSpot,
    times: Sequence[datetime],
    span_end: datetime,
    steps: Sequence[TimelineStepOut],
) -> list[TimelineDayOut]:
    zone = ZoneInfo(spot.timezone)
    first = times[0].astimezone(zone).date()
    last = (span_end - timedelta(microseconds=1)).astimezone(zone).date()
    good = min_score_for_label(QualityLabel.GOOD)
    good_mask = [s.quality.score >= good for s in steps]
    extents = timesteps.centred_extents(times, [s.step_hours for s in steps])
    out: list[TimelineDayOut] = []
    day = first
    while day <= last:
        start, end = timesteps.local_day_bounds(day, spot.timezone)
        sunrise, sunset, state = _sun_events(spot.latitude, spot.longitude, start, end, day)
        # Light from the evening before and the morning after counts for steps whose
        # interval crosses midnight; each step is clipped to this day below.
        around = (start - timedelta(days=1), end + timedelta(days=1))
        windows = daylight_windows(spot.latitude, spot.longitude, *around)
        sunlit = daylight_windows(spot.latitude, spot.longitude, *around, margin=timedelta(0))
        daylight = [
            s
            for s, extent in zip(steps, extents, strict=True)
            if start <= s.t < end
            and s.is_daylight
            and timesteps.mostly_inside(extent, sunlit, BEST_MIN_LIGHT_SHARE)
        ]
        top = max(daylight, key=lambda s: s.quality.score) if daylight else None
        out.append(
            TimelineDayOut(
                date=day,
                start_utc=start,
                end_utc=end,
                sunrise=sunrise,
                sunset=sunset,
                first_light=sunrise - TWILIGHT_MARGIN if sunrise else None,
                last_light=sunset + TWILIGHT_MARGIN if sunset else None,
                polar_state=state,  # type: ignore[arg-type]
                best_time=top.t if top else None,
                best_score=top.quality.score if top else None,
                best_label=top.quality.label if top else None,
                good_hours=round(
                    timesteps.covered_hours(extents, good_mask, windows, (start, end)), 2
                ),
                forecast_confidence_class=top.quality.forecast_confidence_class if top else None,
            )
        )
        day += timedelta(days=1)
    return out


def run_members(db: Session, run: ForecastRun) -> list[RunMemberOut]:
    """The model runs behind ``run``: a blend's members (from its details), otherwise the
    run itself."""
    if run.source.code != BLEND_CODE:
        return [_member(run.source, run.run_key, run.issued_at)]
    members = [m for m in (run.details or {}).get("members", []) if m.get("code")]
    codes = [str(m["code"]) for m in members]
    sources = {
        s.code: s
        for s in db.scalars(select(ForecastSource).where(ForecastSource.code.in_(codes))).all()
    }
    out: list[RunMemberOut] = []
    for m in members:
        source = sources.get(str(m["code"]))
        if source is None:
            continue
        issued = m.get("issued_at")
        out.append(
            _member(
                source,
                str(m.get("run_key") or ""),
                datetime.fromisoformat(str(issued)) if issued else None,
            )
        )
    return out


def _member(source: ForecastSource, run_key: str, init_time: datetime | None) -> RunMemberOut:
    return RunMemberOut(
        code=source.code,
        name=source.name,
        family=source.family,
        run_key=run_key,
        init_time=init_time,
        wave_model=source.wave_model,
        atmosphere_model=source.atmosphere_model,
    )


def _provenance(
    db: Session, spot: SurfSpot, run: ForecastRun, first: SurfQualityPrediction, now: datetime
) -> ProvenanceOut:
    source = run.source
    return ProvenanceOut(
        source=ForecastSourceOut(
            code=source.code,
            name=source.name,
            is_demo=source.is_demo,
            attribution=source_attribution(db, run),
            wave_model=source.wave_model,
            atmosphere_model=source.atmosphere_model,
            attribution_links=source_attribution_links(db, run),
        ),
        run=TimelineRunOut(
            run_key=run.run_key,
            init_time=run.issued_at,
            completed_at=run.completed_at,
            status=run.status.value,
            age_hours=round(_hours(now - run.issued_at), 2),
            # A blend has no limit of its own: it is stale when any member is (``stale``).
            stale_after_hours=None if source.code == BLEND_CODE else stale_after_hours(source.code),
        ),
        members=run_members(db, run),
        breaking_method=first.breaking_height_method,
        algorithm_version=first.algorithm_version,
        calibrated=bool(spot.calibration),
    )


def _blocks(
    db: Session, spot: SurfSpot, start: datetime, end: datetime, now: datetime
) -> tuple[TideBlockOut, ObservationsBlockOut]:
    lookback = timedelta(hours=get_settings().observation_lookback_hours)
    return (
        spot_tide_block(db, spot, start, end, now),
        spot_observations_block(db, spot, now - lookback, now, now),
    )


def _unavailable(
    db: Session, spot: SurfSpot, days: int, now: datetime, message: str
) -> SpotTimelineOut:
    tide, observations = _blocks(db, spot, now, now + timedelta(days=days), now)
    return SpotTimelineOut(
        spot_slug=spot.slug,
        timezone=spot.timezone,
        generated_at=now,
        available=False,
        message=message,
        stale=False,
        units=TimelineUnitsOut(),
        steps_meta=StepsMetaOut(segments=[]),
        provenance=None,
        days=[],
        steps=[],
        tide=tide,
        observations=observations,
    )


def spot_timeline(
    db: Session, spot: SurfSpot, days: int = 10, now: datetime | None = None
) -> SpotTimelineOut:
    """The timeline of ``spot`` for the next ``days`` days (see the module docstring)."""
    now = now or utcnow()
    run = latest_runs_by_spot(db, now).get(spot.id)
    if run is None:
        return _unavailable(db, spot, days, now, NO_FORECAST)
    # The same hours as /forecast's (one read when both are computed together).
    rows = spot_hours(db, run.id, spot.id, now - LOOKBACK, now + timedelta(days=days))
    if not rows:
        return _unavailable(db, spot, days, now, NO_FUTURE_DATA)

    times = [p.valid_time for p, _ in rows]
    # The last step lasts until the run's next stored time, beyond the window.
    after = db.scalar(
        select(SurfQualityPrediction.valid_time)
        .where(
            SurfQualityPrediction.run_id == run.id,
            SurfQualityPrediction.spot_id == spot.id,
            SurfQualityPrediction.valid_time > times[-1],
        )
        .order_by(SurfQualityPrediction.valid_time)
        .limit(1)
    )
    # A window holding a single step at the end of the run: its length is the gap from the
    # stored time before it (the run's own spacing), not a guess.
    before = (
        db.scalar(
            select(SurfQualityPrediction.valid_time)
            .where(
                SurfQualityPrediction.run_id == run.id,
                SurfQualityPrediction.spot_id == spot.id,
                SurfQualityPrediction.valid_time < times[0],
            )
            .order_by(SurfQualityPrediction.valid_time.desc())
            .limit(1)
        )
        if after is None and len(times) == 1
        else None
    )
    durations = step_durations(times, after, before)
    tide, observations = _blocks(
        db, spot, times[0], times[-1] + timedelta(hours=durations[-1]), now
    )
    return assemble_timeline(
        spot,
        rows,
        after=after,
        before=before,
        stale=is_stale(run, now),
        provenance=_provenance(db, spot, run, rows[0][0], now),
        tide=tide,
        observations=observations,
        stages=tide_stages_for(db, spot.id, times),
        now=now,
    )


def step_durations(
    times: Sequence[datetime], after: datetime | None = None, before: datetime | None = None
) -> list[float]:
    """Hours each step stands for: the gap to the next stored time (``timesteps.step_hours``).

    The last step lasts until ``after`` (the run's next stored time beyond the window) when
    there is one; at the end of the run's horizon it keeps the spacing of the steps before
    it, not the series' median (in a 3-hourly-then-6-hourly run the last step is 6 h). A
    lone step at the end of the horizon keeps the gap from ``before`` (the run's stored time
    before it), and 0 h when the run has no other time at all.
    """
    if not times:
        return []
    hours = timesteps.forward_hours(times, after)
    if after is None and len(times) == 1 and before is not None and before < times[0]:
        hours = [_hours(times[0] - before)]
    return hours


def step_segments(
    times: Sequence[datetime], durations: Sequence[float]
) -> list[timesteps.StepSegment]:
    """Runs of equal step durations, as ``timesteps.segments`` merges them, with the given
    durations (see :func:`step_durations`)."""
    out: list[timesteps.StepSegment] = []
    for t, hours in zip(times, durations, strict=True):
        end = t + timedelta(hours=hours)
        if out and abs(out[-1].step_hours - hours) <= 1e-6:
            out[-1] = timesteps.StepSegment(out[-1].start, end, out[-1].step_hours)
        else:
            out.append(timesteps.StepSegment(t, end, hours))
    return out


def assemble_timeline(
    spot: SurfSpot,
    rows: Sequence[tuple[SurfQualityPrediction, WaveForecast]],
    *,
    after: datetime | None = None,
    before: datetime | None = None,
    stale: bool,
    provenance: ProvenanceOut | None,
    tide: TideBlockOut,
    observations: ObservationsBlockOut,
    stages: dict[datetime, TideStage],
    now: datetime,
) -> SpotTimelineOut:
    """The timeline from stored rows (prediction, forecast) sorted by valid time, plus the
    tide and observation blocks: no database access, so it is unit-tested directly.
    ``after`` is the run's next stored valid time beyond the rows, if any, and ``before`` the
    one before a lone row (see :func:`step_durations`)."""
    times = [p.valid_time for p, _ in rows]
    durations = step_durations(times, after, before)
    segments = step_segments(times, durations)
    steps = [
        _step(p, f, hours, stale, tide, stages.get(p.valid_time))
        for (p, f), hours in zip(rows, durations, strict=True)
    ]
    return SpotTimelineOut(
        spot_slug=spot.slug,
        timezone=spot.timezone,
        generated_at=now,
        available=bool(steps),
        message=None if steps else NO_FUTURE_DATA,
        stale=stale,
        units=TimelineUnitsOut(),
        steps_meta=StepsMetaOut(
            segments=[
                StepSegmentOut(start=s.start, end=s.end, step_hours=s.step_hours) for s in segments
            ],
            interpolation="some_steps" if any(s.interpolated for s in steps) else "none",
        ),
        provenance=provenance,
        days=_days(spot, times, segments[-1].end, steps) if steps else [],
        steps=steps,
        tide=tide,
        observations=observations,
    )


def _step(
    p: SurfQualityPrediction,
    f: WaveForecast,
    hours: float,
    stale: bool,
    tide: TideBlockOut,
    stage: TideStage | None,
) -> TimelineStepOut:
    return TimelineStepOut(
        t=p.valid_time,
        step_hours=hours,
        lead_hours=_r(p.lead_hours, 1),
        is_daylight=p.is_daylight,
        interpolated=bool((f.extra or {}).get(EXTRA_INTERPOLATED, False)),
        interpolated_members=[str(c) for c in (f.extra or {}).get("interpolated_members") or []],
        quality=_quality(p, stale),
        breaking=StepBreakingOut(
            # Rows scored before the central value was stored: their typical height is that
            # central value rounded to 0.5 ft.
            central_ft=(
                p.breaking_height_mid_ft
                if p.breaking_height_mid_ft is not None
                else p.breaking_height_min_ft
            ),
            typical_ft=p.breaking_height_min_ft,
            sets_ft=p.breaking_height_max_ft,
            p10_ft=p.breaking_p10_ft,
            p90_ft=p.breaking_p90_ft,
            uncertainty_kind=p.uncertainty_kind,
        ),
        offshore=StepOffshoreOut(
            hs_m=_r(f.sig_wave_height_m, 3),
            tp_s=_r(f.wave_period_s, 2),
            period_kind=f.wave_period_kind,
            dir_from_deg=_r(f.wave_direction_deg, 1),
        ),
        partitions=_partitions(p, f),
        wind=_wind(p, f),
        tide=_tide_at(p.valid_time, tide, stage),
        why=_why(p),
    )


def timeline_etag(timeline: SpotTimelineOut, days: int) -> str:
    """A weak validator for the response: it changes when the run, the scoring algorithm,
    the tide predictions, the first step in the window, the staleness, the newest
    observation or a step's rating changes (``generated_at`` and the run's age are not part
    of it). The ratings are included because a re-score after new research evidence
    (``predict.outdated_authoritative_runs``) rewrites the shown run's predictions under the
    same run key and algorithm version."""
    provenance = timeline.provenance
    parts: list[Any] = [
        provenance.run.run_key if provenance else "",
        provenance.algorithm_version if provenance else "",
        timeline.tide.fetched_at.isoformat() if timeline.tide.fetched_at else "",
        timeline.tide.status,
        timeline.tide.preference.text,
        provenance.calibrated if provenance else "",
        timeline.steps[0].t.isoformat() if timeline.steps else "",
        timeline.steps[-1].t.isoformat() if timeline.steps else "",
        timeline.stale,
        timeline.available,
        days,
        max((o.t for o in timeline.observations.items), default=None),
        timeline.observations.status,
        *(
            (
                s.quality.score,
                s.quality.label,
                s.quality.forecast_confidence_class,
                s.quality.local_confidence_class,
                s.breaking.central_ft,
                s.breaking.sets_ft,
                s.wind.favourability if s.wind else None,
            )
            for s in timeline.steps
        ),
    ]
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode(), usedforsecurity=False)
    return f'W/"{digest.hexdigest()}"'
