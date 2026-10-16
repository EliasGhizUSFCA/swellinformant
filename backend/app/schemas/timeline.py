"""The spot forecast timeline: ``GET /api/spots/{slug}/timeline?days=10``.

One response holds everything the spot page's synchronised charts draw, on one time axis:
the quality strip, breaking-height bars, swell partitions, wind, tide and the
selected-time summary. Rules of the contract:

* ``steps`` are the stored forecast valid times of the authoritative run (the same run as
  ``/forecast``), at the model's native spacing; the timeline adds no values between
  them. ``step_hours`` is the gap to the next valid time (``steps_meta.segments`` lists
  where the spacing changes, e.g. hourly, then 3-hourly). A chart picks the nearest valid
  step for a selected time, so a step stands for the interval halfway back to the previous
  valid time and halfway on to the next.
* A blend step can hold a member's value interpolated linearly in time between that
  member's own steps (ECMWF is 3-hourly where the GFS family is hourly), or a value its
  source interpolated: such a step has ``interpolated`` true and names the members in
  ``interpolated_members``, and ``steps_meta.interpolation`` is "some_steps".
* Three kinds of numbers stay apart: raw offshore model output (``offshore`` and
  ``partitions``), our breaking-height prediction (``breaking``) and our quality rating
  (``quality``).
* Units are fixed (``units``): breaking heights in feet, offshore heights and tide in
  metres, wind in km/h. The client converts for display.
* Directions are degrees FROM (nautical). Confidence classes are documented heuristic
  classes, not probabilities.
* ``days`` are local calendar days in ``timezone``, from local midnight to local midnight
  (23 or 25 hours on daylight-saving change days).
* ``available`` false: ``message`` says why, ``steps`` and ``days`` are empty and
  ``provenance`` is None; the tide and observation blocks still state their own status.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.spots import ForecastSourceOut, RunMemberOut
from app.schemas.tides import ObservationsBlockOut, TideBlockOut

PartitionKind = Literal["primary_swell", "secondary_swell", "tertiary_swell", "wind_sea"]
PolarState = Literal["normal", "polar_day", "polar_night"]


class TimelineUnitsOut(BaseModel):
    breaking: Literal["ft"] = "ft"
    offshore: Literal["m"] = "m"
    wind: Literal["km/h"] = "km/h"
    tide: Literal["m"] = "m"


class StepSegmentOut(BaseModel):
    """A stretch of steps with the same spacing: ``start`` is the first step of the stretch,
    ``end`` its last step plus one step."""

    start: datetime
    end: datetime
    step_hours: float


class StepsMetaOut(BaseModel):
    segments: list[StepSegmentOut]
    # "none": no step holds an interpolated value; "some_steps": the steps with
    # ``interpolated`` true do (a blend member aligned in time, or the source's own).
    interpolation: Literal["none", "some_steps"] = "none"
    policy: Literal["nearest-valid-step"] = "nearest-valid-step"


class TimelineRunOut(BaseModel):
    run_key: str
    init_time: datetime | None
    completed_at: datetime | None
    status: str
    # Age of the model run (now - init_time) and the source's stale limit, in hours.
    age_hours: float | None
    stale_after_hours: float | None


class ProvenanceOut(BaseModel):
    source: ForecastSourceOut
    run: TimelineRunOut
    members: list[RunMemberOut] = []
    breaking_method: str | None
    algorithm_version: str | None
    calibrated: bool


class TimelineDayOut(BaseModel):
    date: date  # local calendar date in the spot's time zone
    start_utc: datetime  # local midnight
    end_utc: datetime  # the next local midnight (23 or 25 h later on DST days)
    sunrise: datetime | None
    sunset: datetime | None
    first_light: datetime | None
    last_light: datetime | None
    polar_state: PolarState
    best_time: datetime | None
    best_score: int | None
    best_label: str | None
    # Hours between first and last light of this day covered by the intervals of Good or
    # better steps (each step standing for its nearest-valid-step interval).
    good_hours: float
    forecast_confidence_class: str | None


class StepQualityOut(BaseModel):
    score: int
    label: str
    forecast_confidence_class: str | None
    local_confidence_class: str | None
    forecast_confidence_reasons: list[str]
    local_confidence_reason: str | None


class StepBreakingOut(BaseModel):
    """Our predicted breaking (face) height at the break, in feet."""

    central_ft: float  # the bar: breaking_height_mid_ft
    typical_ft: float
    sets_ft: float
    p10_ft: float | None
    p90_ft: float | None
    uncertainty_kind: str | None  # "heuristic" | "ensemble_heuristic" | "calibrated"


class StepOffshoreOut(BaseModel):
    """Raw offshore model sea state (significant height, not surf height)."""

    hs_m: float | None
    tp_s: float | None
    period_kind: str | None  # "tp" | "tm01" | "tm02" | "tm10"
    dir_from_deg: float | None


class PartitionOut(BaseModel):
    """One wave system of the model; only systems the model provides are listed."""

    kind: PartitionKind
    height_m: float
    period_s: float | None
    dir_from_deg: float | None
    # Our breaking-height estimate for this system alone, when computed.
    breaking_ft: float | None
    # The system that drives the predicted surf (breaking.dominant_component).
    drives_surf: bool


GustCause = Literal["source", "blend"]
WindDisagreement = Literal["direction", "speed"]


class StepWindOut(BaseModel):
    speed_kmh: float | None
    gust_kmh: float | None
    gust_kind: str | None
    # The gust shown is below the mean wind shown. Cause "source": the gust's own model put
    # it below its own mean wind (a model artefact); "blend": the gust comes from fewer
    # models than the blended mean and is not below their own mean.
    gust_below_mean: bool
    gust_below_mean_cause: GustCause | None = None
    # Independent models of a blend disagree on the wind direction (by more than 60 deg,
    # both at 10 km/h or more) or speed (by more than 15 km/h) at this step.
    models_disagree: list[WindDisagreement] = []
    dir_from_deg: float | None
    # One of the six stored wind_relation strings (offshore, cross-offshore, ...).
    relation: str
    strength: str | None  # "glassy" | "light" | "moderate" | "strong"
    favourability: str | None  # "favourable" | "workable" | "unfavourable"
    fit: float | None  # wind score 0..1
    offshore_component_kmh: float | None  # V . n (positive = offshore)
    cross_component_kmh: float | None


class StepTideOut(BaseModel):
    # Read from the tide curve at the step's time (never from model sea level).
    height_m: float
    rising: bool | None
    stage: float | None  # 0 at the preceding low water, 1 at the next high water


class StepWhyOut(BaseModel):
    """What drove the score: factor fits and gates (0..1), points lost per factor."""

    fits: dict[str, float]
    gates: dict[str, float]
    contributions: dict[str, float] | None
    tide_included: bool
    explanation: str


class TimelineStepOut(BaseModel):
    t: datetime
    step_hours: float
    lead_hours: float | None
    is_daylight: bool
    # A value of this step was interpolated in time: a blend member aligned between its own
    # steps (members in ``interpolated_members``) or a value the source interpolated.
    interpolated: bool
    interpolated_members: list[str] = []
    quality: StepQualityOut
    breaking: StepBreakingOut
    offshore: StepOffshoreOut
    partitions: list[PartitionOut]
    wind: StepWindOut | None
    tide: StepTideOut | None
    why: StepWhyOut


class SpotTimelineOut(BaseModel):
    spot_slug: str
    timezone: str  # IANA name
    generated_at: datetime  # server time (UTC) when the response was built
    available: bool
    message: str | None = None
    stale: bool
    units: TimelineUnitsOut
    steps_meta: StepsMetaOut
    provenance: ProvenanceOut | None = None
    days: list[TimelineDayOut]
    steps: list[TimelineStepOut]
    tide: TideBlockOut
    observations: ObservationsBlockOut
