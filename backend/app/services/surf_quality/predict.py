"""Job B — score every forecast timestep of a model run.

Every prediction also gets its confidence (methodology forecast-v1.2,
``forecast_confidence``): forecast confidence of the offshore forecast, with the inputs and
rules used, the run's issue time, the lead time and the spot's research assessment (its
overall research class is the hour's research confidence, kept separate from forecast
confidence). The combined local confidence is still stored for the deprecated API field;
no decision uses it (spec 20). Algorithm 2.2.0 also stores the hour's favourable and
unfavourable conditions and scores with the SCORING_WEIGHTS_JSON weights, if set.

Where sources disagree about which wind is offshore (the spot's research assessment
fires a conflict rule on wind, or data/surf_spots/profile_decisions.json records the
offshore direction as disputed), the hour also stores the wind relation under the main
contradicting direction (``components.wind_dispute``, ``confidence_inputs.wind_dispute``),
its explanation names both relations when they differ, and its confidence reasons say
"Sources disagree about which wind is offshore here." The score keeps the profile's
direction. Decision records are read from the data directory once per scoring pass; a
change to that file alone takes effect at the next rescoring.

The tide stage of each hour comes from the stored tide predictions
(``tides.stage.tide_stages_for``: 0 at the preceding low water, 1 at the next high water),
never from model sea level; demo runs without tide predictions use their synthetic sea level
(labelled source "demo"). The tide is scored only when the spot's tide research class is
moderate or high, its tide decision is not disputed and it has a preference; otherwise the
stored components say why (``tide_excluded_reason``). Gusts enter the wind fit only with
WIND_GUST_SCORING=scored and a documented gust definition.

Database egress (a pass runs after every model run, docs/map-layers.md): a run's forecasts
are read with the columns and ``extra`` keys scoring uses (:class:`ScoringForecast`), and
:func:`compute_pending_predictions` reads what every run of the pass shares once
(:class:`PassReads`: the spots, research inputs, profile decisions and each spot's tide
predictions, ``tides.stage.shared_tide_reads``). The scores are unchanged.

Lean imports (Swell 2.0, design D8): the job runs in the refresh Function, whose install has
no pandas, scikit-learn, SciPy, pyarrow, joblib or ecCodes. The demo tide stages are plain
Python (value for value the pandas rolling window they replaced) and the optional ML
correction (``ml.hook``) is imported only when ML_CORRECTIONS_ENABLED and an ACTIVE model
version exist (:func:`run_corrector`); tests/unit/test_lean_imports.py and
tests/integration/test_lean_predict.py check it.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import (
    ForecastEnsembleStat,
    ForecastRun,
    ForecastSource,
    MLModelVersion,
    SpotResearchAssessment,
    SurfQualityPrediction,
    SurfSpot,
    WaveForecast,
)
from app.models.enums import ForecastRunStatus, MLModelStatus
from app.services.research.decisions import (
    DecisionValidationError,
    WindDispute,
    load_decisions,
    record_for,
    wind_dispute,
)
from app.services.surf_quality.confidence import completeness, confidence_label, run_agreement
from app.services.surf_quality.confidence_context import (
    AssessmentInputs,
    RunConfidenceContext,
    research_inputs,
    scoring_assessments,
    station_codes,
)
from app.services.surf_quality.forecast_confidence import (
    METHOD as CONFIDENCE_METHOD,
)
from app.services.surf_quality.forecast_confidence import (
    EnsembleSignal,
    ForecastInputs,
    ResearchBasisInput,
    classify_forecast,
    classify_local,
    confidence_inputs,
    run_change_ratio,
    wind_dispute_inputs,
)
from app.services.surf_quality.models import EXTRA_GUST_KIND, SeaState, SpotParams
from app.services.surf_quality.scoring import (
    disputed_wind_relation,
    evaluate,
    model_version,
)
from app.services.surf_quality.sun import is_daylight
from app.services.surf_quality.uncertainty import (
    EnsembleInfo,
    breaking_uncertainty,
    confidence_index,
)
from app.services.surf_quality.weights import effective_weights
from app.services.tides.stage import TideStage, shared_tide_reads, tide_stages_for

if TYPE_CHECKING:
    from app.services.ml.hook import RunCorrector

logger = logging.getLogger(__name__)

INSERT_CHUNK = 2000
MIN_TIDAL_RANGE_M = 0.15
# A demo run's tide stage: the level against the lowest and highest level within half this
# window either side, given at least this many known levels.
TIDE_WINDOW = timedelta(hours=25)
TIDE_WINDOW_MIN_LEVELS = 6
# An ensemble row is used for a forecast hour only if it is this close in time, and only
# from an ensemble run issued within ENSEMBLE_RUN_WINDOW of the forecast run.
ENSEMBLE_MAX_GAP = timedelta(hours=3)
ENSEMBLE_RUN_WINDOW = timedelta(hours=24)


def _missing(value: float | None) -> bool:
    return value is None or math.isnan(value)


def tide_stages(times: list[datetime], levels: list[float | None]) -> list[float | None]:
    """Position in the local tidal cycle: 0 = low, 1 = high, against the lowest and highest
    level within ±12.5 h.

    The window of a time t is ``(t - 12.5 h, t + 12.5 h]`` and needs at least
    TIDE_WINDOW_MIN_LEVELS known levels; a stage is None without its own level, without
    enough levels or when the window's range is below MIN_TIDAL_RANGE_M. This is, value for
    value, the pandas rolling window it replaced (``rolling("25h", center=True,
    min_periods=6)`` min and max, closed on the right), in plain Python so that scoring
    imports no data library (tests/unit/test_tide_stages.py compares the two)."""
    if not times or all(_missing(v) for v in levels):
        return [None] * len(times)
    order = sorted(range(len(times)), key=times.__getitem__)
    at = [times[i] for i in order]
    known: list[float | None] = []
    for i in order:
        v = levels[i]
        known.append(None if v is None or math.isnan(v) else float(v))
    half = TIDE_WINDOW / 2
    stages: list[float | None] = [None] * len(times)
    first = last = 0
    for k, t in enumerate(at):
        while at[first] <= t - half:
            first += 1
        while last < len(at) and at[last] <= t + half:
            last += 1
        level = known[k]
        window = [v for v in known[first:last] if v is not None]
        if level is None or len(window) < TIDE_WINDOW_MIN_LEVELS:
            continue
        lo, hi = min(window), max(window)
        span = hi - lo
        if span < MIN_TIDAL_RANGE_M:
            continue
        stages[order[k]] = round((level - lo) / span, 3)
    return stages


DEMO_TIDE_SOURCE = "demo"


def demo_tide_stages(
    times: Sequence[datetime], levels: Sequence[float | None]
) -> dict[datetime, TideStage]:
    """Tide stages of a DEMO run from its synthetic sea level (``tide_stages`` above, with
    rising from the neighbouring levels). Never used for real forecasts: their sea level has
    no datum and model sea level is not a tide prediction."""
    stages = tide_stages(list(times), list(levels))
    out: dict[datetime, TideStage] = {}
    for i, (t, stage) in enumerate(zip(times, stages, strict=True)):
        level = levels[i]
        if stage is None or level is None:
            continue
        nxt = levels[i + 1] if i + 1 < len(levels) else None
        prev = levels[i - 1] if i > 0 else None
        rising = nxt > level if nxt is not None else (prev is not None and level > prev)
        window = [v for v in levels[max(0, i - 6) : i + 7] if v is not None]
        out[t] = TideStage(
            stage=stage,
            rising=bool(rising),
            range_m=round(max(window) - min(window), 3),
            source=DEMO_TIDE_SOURCE,
        )
    return out


def tide_research_classes(
    db: Session, assessments: dict[int, AssessmentInputs] | None = None
) -> dict[int, str | None]:
    """spot_id -> the tide class ("high" | "moderate" | "low") of its current research
    assessment; spots without an assessment are absent. ``assessments``:
    ``confidence_context.scoring_assessments``, when the caller has read them already."""
    out: dict[int, str | None] = {}
    rows = scoring_assessments(db) if assessments is None else assessments
    for spot_id, row in rows.items():
        tide = (row.per_variable or {}).get("tide")
        out[spot_id] = str(tide["class"]) if isinstance(tide, dict) and tide.get("class") else None
    return out


def scoring_params(
    spot: SurfSpot,
    tide_classes: dict[int, str | None],
    decisions: dict[str, list[dict[str, Any]]],
) -> SpotParams:
    """The spot's scoring parameters, with the tide research class and tide decision that
    decide whether its tide preference is scored."""
    record = record_for(decisions, spot.slug, "tide_preference")
    return replace(
        SpotParams.from_orm(spot),
        tide_research_class=tide_classes.get(spot.id),
        tide_decision=str(record["decision"]) if record is not None else None,
    )


def _tide_detail(stage: TideStage | None) -> dict[str, Any] | None:
    if stage is None:
        return None
    return {
        "stage": round(stage.stage, 3),
        "rising": stage.rising,
        "range_m": round(stage.range_m, 3),
        "source": stage.source,
    }


EnsembleIndex = dict[int, list[tuple[datetime, EnsembleInfo]]]


def _ensemble_index(
    db: Session,
    run: ForecastRun,
    cache: dict[int, EnsembleIndex] | None = None,
) -> EnsembleIndex:
    """Ensemble rows per spot from the newest ensemble run close to this run's issue time.
    ``cache``: ensemble run id -> its index, shared by the runs of one pass (the runs of a
    cycle find the same ensemble run)."""
    ens_run_id = db.scalar(
        select(ForecastRun.id)
        .join(ForecastSource, ForecastSource.id == ForecastRun.source_id)
        .where(
            ForecastSource.kind == "ensemble",
            ForecastRun.status.in_((ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL)),
            ForecastRun.issued_at <= run.issued_at + ENSEMBLE_RUN_WINDOW,
            ForecastRun.issued_at >= run.issued_at - ENSEMBLE_RUN_WINDOW,
        )
        .order_by(ForecastRun.issued_at.desc(), ForecastRun.id.desc())
        .limit(1)
    )
    out: EnsembleIndex = defaultdict(list)
    if ens_run_id is None:
        return out
    if cache is not None and ens_run_id in cache:
        return cache[ens_run_id]
    e = ForecastEnsembleStat
    for spot_id, valid_time, hs_mean, hs_spread, tp_mean, tp_spread, members in db.execute(
        select(
            e.spot_id,
            e.valid_time,
            e.hs_mean_m,
            e.hs_spread_m,
            e.tp_mean_s,
            e.tp_spread_s,
            e.members,
        )
        .where(e.run_id == ens_run_id)
        .order_by(e.spot_id, e.valid_time)
    ):
        out[spot_id].append(
            (
                valid_time,
                EnsembleInfo(
                    hs_mean_m=hs_mean,
                    hs_spread_m=hs_spread,
                    tp_mean_s=tp_mean,
                    tp_spread_s=tp_spread,
                    members=members,
                ),
            )
        )
    if cache is not None:
        cache[ens_run_id] = out
    return out


def _nearest_ensemble(
    rows: list[tuple[datetime, EnsembleInfo]], t: datetime
) -> EnsembleInfo | None:
    best: tuple[timedelta, EnsembleInfo] | None = None
    for vt, info in rows:
        gap = abs(vt - t)
        if gap <= ENSEMBLE_MAX_GAP and (best is None or gap < best[0]):
            best = (gap, info)
    return best[1] if best else None


def _profile_decisions() -> dict[str, list[dict[str, Any]]]:
    """The profile decision records (slug -> records); empty when the file is missing or
    invalid (an invalid file is logged, never fatal for scoring)."""
    try:
        return load_decisions()
    except (DecisionValidationError, OSError, ValueError) as exc:
        logger.warning("Profile decisions not used: %s", exc)
        return {}


def run_corrector(db: Session, run: ForecastRun) -> RunCorrector | None:
    """The ML correction of offshore Hs for this run (``ml.hook``), or None.

    ML corrections are optional and off by default (ML_CORRECTIONS_ENABLED=false). The ML
    code (scikit-learn, joblib, pandas) is imported only when the setting is on AND an ACTIVE
    model version exists, so scoring imports and runs without the data libraries otherwise;
    ``RunCorrector.for_run`` then decides as before whether that model applies to this run.
    """
    if not get_settings().ml_corrections_enabled:
        return None
    active = exists().where(MLModelVersion.status == MLModelStatus.ACTIVE)
    if not db.scalar(select(active)):
        return None
    from app.services.ml.hook import RunCorrector

    return RunCorrector.for_run(db, run)


def spot_wind_dispute(
    spot: SurfSpot,
    research: ResearchBasisInput | None,
    decisions: dict[str, list[dict[str, Any]]],
) -> WindDispute | None:
    """Whether sources disagree about the offshore wind at ``spot`` (see the docstring)."""
    record = record_for(decisions, spot.slug, "offshore_wind_direction")
    per_variable = {"wind": research.wind} if research is not None and research.wind else None
    return wind_dispute(per_variable, spot.offshore_wind_direction, record)


@dataclass(frozen=True, slots=True)
class ScoringForecast:
    """The ``wave_forecasts`` columns scoring reads, with ``extra`` holding only the keys it
    reads (the gust definition and a blend's ML corrections): a whole row also carries the
    run, issue time, pressure, wave power, missing-field list and the rest of ``extra``,
    about half its size."""

    id: int
    spot_id: int
    valid_time: datetime
    sig_wave_height_m: float | None
    wave_period_s: float | None
    wave_period_kind: str | None
    wave_direction_deg: float | None
    primary_swell_height_m: float | None
    primary_swell_period_s: float | None
    primary_swell_direction_deg: float | None
    secondary_swell_height_m: float | None
    secondary_swell_period_s: float | None
    secondary_swell_direction_deg: float | None
    tertiary_swell_height_m: float | None
    tertiary_swell_period_s: float | None
    tertiary_swell_direction_deg: float | None
    wind_wave_height_m: float | None
    wind_wave_period_s: float | None
    wind_wave_direction_deg: float | None
    wind_speed_kmh: float | None
    wind_direction_deg: float | None
    wind_gust_kmh: float | None
    sea_level_m: float | None
    extra: dict[str, Any]


# ``extra`` keys scoring reads: SeaState's gust definition and _ml_correction's record.
SCORING_EXTRA_KEYS = (EXTRA_GUST_KIND, "ml_corrections")
_SCORING_COLUMNS = [name for name in ScoringForecast.__dataclass_fields__ if name != "extra"]


def scoring_forecasts(db: Session, run_id: int) -> list[ScoringForecast]:
    """The run's forecasts for scoring, by spot and valid time. ``extra`` holds a read key
    only when its stored value is not null: scoring reads it with ``dict.get``, which gives
    None for an absent key and for a JSON null alike."""
    w = WaveForecast
    columns = [getattr(w, name) for name in _SCORING_COLUMNS]
    extras = [w.extra[key].label(f"extra_{key}") for key in SCORING_EXTRA_KEYS]
    out: list[ScoringForecast] = []
    for row in db.execute(
        select(*columns, *extras).where(w.run_id == run_id).order_by(w.spot_id, w.valid_time)
    ):
        values = row._mapping
        extra = {
            key: values[f"extra_{key}"]
            for key in SCORING_EXTRA_KEYS
            if values[f"extra_{key}"] is not None
        }
        out.append(
            ScoringForecast(**{name: values[name] for name in _SCORING_COLUMNS}, extra=extra)
        )
    return out


@dataclass
class PassReads:
    """What every run of one scoring pass reads the same way, read once for the pass: the
    spots (with their profiles), the profile decisions, the research inputs and tide
    research classes, and the ensemble index of each ensemble run used."""

    spots: dict[int, SurfSpot]
    decisions: dict[str, list[dict[str, Any]]]
    tide_classes: dict[int, str | None]
    research: dict[int, ResearchBasisInput]
    ensembles: dict[int, EnsembleIndex] = field(default_factory=dict)

    @classmethod
    def load(cls, db: Session) -> PassReads:
        assessments = scoring_assessments(db)
        return cls(
            spots={s.id: s for s in db.scalars(select(SurfSpot)).all()},
            decisions=_profile_decisions(),
            tide_classes=tide_research_classes(db, assessments),
            research=research_inputs(assessments),
        )


def _missing_swell(f: ScoringForecast) -> bool:
    """No period or no direction for the swell (nor the bulk sea the model falls back to)."""
    period = f.primary_swell_period_s if f.primary_swell_period_s is not None else f.wave_period_s
    direction = (
        f.primary_swell_direction_deg
        if f.primary_swell_direction_deg is not None
        else f.wave_direction_deg
    )
    return period is None or direction is None


def compute_predictions_for_run(
    db: Session,
    run_id: int,
    force: bool = False,
    now: datetime | None = None,
    *,
    shared: PassReads | None = None,
) -> int:
    """Score every forecast hour of the run. ``shared``: the pass's :class:`PassReads`
    (read here when not given)."""
    run = db.get(ForecastRun, run_id)
    if run is None or run.status not in (ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL):
        return 0
    if run.predictions_computed_at and not force:
        return 0
    now = now or utcnow()
    reads = shared if shared is not None else PassReads.load(db)
    # Staleness per source (queries.stale_after_hours), previous run, blend comparisons and
    # verification: read once for the whole run; research assessments once per pass.
    ctx = RunConfidenceContext.load(db, run, now, reads.research)
    stale = ctx.stale

    ensembles = _ensemble_index(db, run, reads.ensembles)
    # An active ML correction of offshore Hs (ml.hook). A blend's members were already
    # corrected before blending, so a blend run is never corrected again here.
    corrector = None if run.source.kind == "blend" else run_corrector(db, run)
    spots = reads.spots
    decisions = reads.decisions
    tide_classes = reads.tide_classes
    settings = get_settings()
    gust_scoring = settings.wind_gust_scoring == "scored"
    weights = effective_weights(settings.scoring_weights)
    by_spot: dict[int, list[ScoringForecast]] = defaultdict(list)
    for row in scoring_forecasts(db, run.id):
        by_spot[row.spot_id].append(row)

    rows: list[dict[str, object]] = []
    for spot_id, forecasts in by_spot.items():
        spot = spots.get(spot_id)
        if spot is None:
            continue
        params = scoring_params(spot, tide_classes, decisions)
        stations = station_codes(spot)
        research = ctx.research.get(spot_id)
        dispute = spot_wind_dispute(spot, research, decisions)
        alternative = dispute.alternative_offshore_deg if dispute is not None else None
        times = [f.valid_time for f in forecasts]
        stages = tide_stages_for(db, spot_id, times)
        if not stages and run.source.is_demo:
            stages = demo_tide_stages(times, [f.sea_level_m for f in forecasts])
        for f in forecasts:
            tide = stages.get(f.valid_time)
            stage = None if tide is None else round(tide.stage, 3)
            sea = SeaState.from_orm(f, tide_stage=stage)
            ml_label, ml_info = _ml_correction(f), (f.extra or {}).get("ml_corrections")
            if corrector is not None:
                hit = corrector.factor(spot.slug, _row_values(f), f.valid_time)
                if hit is not None:
                    sea = sea.scaled(hit[0])
                    ml_label, ml_info = corrector.label, hit[1]
            result = evaluate(
                sea,
                params,
                alternative_offshore_deg=alternative,
                gust_scoring=gust_scoring,
                tide_detail=_tide_detail(tide),
                weights=weights,
            )
            dispute_entry = (
                None
                if dispute is None
                else wind_dispute_inputs(
                    dispute,
                    result.wind_relation,
                    disputed_wind_relation(
                        sea.wind_speed_kmh, sea.wind_direction_deg, params, alternative
                    ),
                )
            )
            lead_hours = (f.valid_time - run.issued_at).total_seconds() / 3600.0
            values = {
                k: getattr(f, k)
                for k in (
                    "primary_swell_height_m",
                    "primary_swell_period_s",
                    "primary_swell_direction_deg",
                    "wind_speed_kmh",
                    "wind_direction_deg",
                )
            }
            prev_hs, prev_swell = ctx.previous.get((spot_id, f.valid_time), (None, None))
            # Diagnostic only (see confidence.py): how much this hour changed since the
            # previous run of the same source.
            agreement = run_agreement(f.primary_swell_height_m, prev_swell)
            ensemble = _nearest_ensemble(ensembles.get(spot_id, []), f.valid_time)
            unc = breaking_uncertainty(
                result.breaking.mid_ft,
                lead_hours,
                ensemble=ensemble,
                between_model_sigma=ctx.between_model_sigma(spot_id, f.valid_time),
                calibration=params.calibration,
            )
            conf = confidence_index(unc.sigma_forecast, completeness(values), stale)
            families = ctx.hour_families(spot_id, f.valid_time)
            forecast_conf = classify_forecast(
                ForecastInputs(
                    index=conf,
                    sigma_source=unc.source,
                    lead_hours=lead_hours,
                    source_code=run.source.code,
                    is_demo=run.source.is_demo,
                    families=families,
                    ensemble=None
                    if ensemble is None
                    else EnsembleSignal(ensemble.hs_mean_m, ensemble.hs_spread_m, ensemble.members),
                    disagreement=ctx.disagreement(spot_id, f.valid_time),
                    run_change_ratio=run_change_ratio(
                        f.sig_wave_height_m
                        if f.sig_wave_height_m is not None
                        else f.primary_swell_height_m,
                        prev_hs if prev_hs is not None else prev_swell,
                    ),
                    verification=ctx.verification.lookup(families, stations, lead_hours),
                    missing_wind=f.wind_speed_kmh is None or f.wind_direction_deg is None,
                    missing_swell=_missing_swell(f),
                    stale=stale,
                    stale_after_hours=ctx.stale_after_hours,
                )
            )
            # Deprecated combined class (spec 20): stored for the API only, never decided on.
            local_conf = classify_local(forecast_conf.cls, research)
            rows.append(
                {
                    "forecast_id": f.id,
                    "run_id": run.id,
                    "spot_id": spot_id,
                    "valid_time": f.valid_time,
                    "is_daylight": is_daylight(spot.latitude, spot.longitude, f.valid_time),
                    "breaking_height_min_ft": result.breaking.min_ft,
                    "breaking_height_max_ft": result.breaking.max_ft,
                    "breaking_height_method": result.breaking.method,
                    "breaking_height_mid_ft": result.breaking.mid_ft,
                    "breaking_p10_ft": unc.p10_ft,
                    "breaking_p90_ft": unc.p90_ft,
                    "uncertainty_kind": unc.kind,
                    "ml_model_version": ml_label,
                    "score": result.score,
                    "label": result.label,
                    "components": {
                        **result.stored_components(),
                        "run_agreement": agreement,
                        "tide_stage": stage,
                        "uncertainty": unc.as_dict(),
                        **({"ml_correction": ml_info} if ml_info else {}),
                        **(
                            {
                                "wind_dispute": {
                                    k: dispute_entry[k]
                                    for k in (
                                        "alternative_offshore_deg",
                                        "relation",
                                        "relation_alternative",
                                        "differs",
                                    )
                                }
                            }
                            if dispute_entry is not None
                            else {}
                        ),
                        "breaking": {
                            "partitions": result.breaking.partitions,
                            "assumptions": result.breaking.assumptions,
                            "depth_limited": result.breaking.depth_limited,
                        },
                    },
                    "wind_relation": result.wind_relation,
                    "confidence": conf,
                    "confidence_label": confidence_label(conf).value,
                    "explanation": result.explanation,
                    "algorithm_version": result.model_version,
                    "forecast_confidence_class": forecast_conf.cls,
                    "local_confidence_class": local_conf.cls,
                    "confidence_method": CONFIDENCE_METHOD,
                    "confidence_inputs": confidence_inputs(
                        forecast_conf, local_conf, wind_dispute=dispute_entry
                    ),
                    "issued_at": run.issued_at,
                    "lead_hours": round(lead_hours, 2),
                    "research_assessment_id": None if research is None else research.assessment_id,
                }
            )
    if rows:
        base = insert(SurfQualityPrediction)
        stmt = (
            base.on_conflict_do_update(
                index_elements=["forecast_id"],
                set_={c: base.excluded[c] for c in rows[0] if c != "forecast_id"},
            )
            if force
            else base.on_conflict_do_nothing(index_elements=["forecast_id"])
        )
        for i in range(0, len(rows), INSERT_CHUNK):
            db.execute(stmt, rows[i : i + INSERT_CHUNK])  # executemany: compiled once, batched
    run.predictions_computed_at = utcnow()
    db.commit()
    logger.info("Computed %d predictions for run %s", len(rows), run.run_key)
    return len(rows)


def current_model_version() -> str:
    """The version new scores are stored with: ALGORITHM_VERSION, tagged when
    SCORING_WEIGHTS_JSON changes the weights (``scoring.model_version``)."""
    return model_version(effective_weights(get_settings().scoring_weights))


def outdated_authoritative_runs(db: Session, now: datetime | None = None) -> list[int]:
    """Runs currently shown to users whose predictions predate the current model version
    (ALGORITHM_VERSION and the weights, :func:`current_model_version`), the confidence
    methodology or the spot's current research assessment.

    After an algorithm upgrade the live forecast is rescored at once instead of waiting
    for the next model cycle; likewise after new research evidence (``research load`` or a
    seed), so the stored research confidence follows the current assessment. Older runs
    keep the version they were issued with (the prediction archive, when enabled, is the
    record of what was forecast). "Shown to users" is the published forecast version's runs
    (``versions.serving_runs``; before any version, the legacy selection of the public reads).
    """
    from app.services.forecasts.versions import serving_runs

    run_ids = sorted(set(serving_runs(db, now=now).values()))
    if not run_ids:
        return []
    # Each spot's current research assessment; no row = never assessed (NULL on both sides).
    current_research = (
        select(SpotResearchAssessment.spot_id, SpotResearchAssessment.id)
        .where(SpotResearchAssessment.is_current)
        .subquery()
    )
    return list(
        db.scalars(
            select(SurfQualityPrediction.run_id)
            .outerjoin(
                current_research, current_research.c.spot_id == SurfQualityPrediction.spot_id
            )
            .where(
                SurfQualityPrediction.run_id.in_(run_ids),
                (SurfQualityPrediction.algorithm_version != current_model_version())
                | SurfQualityPrediction.confidence_method.is_distinct_from(CONFIDENCE_METHOD)
                | SurfQualityPrediction.research_assessment_id.is_distinct_from(
                    current_research.c.id
                ),
            )
            .distinct()
        ).all()
    )


def _scored_windows(db: Session, run_ids: Sequence[int]) -> dict[int, tuple[datetime, datetime]]:
    """spot_id -> the first and last valid time the runs score there (the window the
    pass's tide predictions are read for)."""
    if not run_ids:
        return {}
    w = WaveForecast
    return {
        spot_id: (lo, hi)
        for spot_id, lo, hi in db.execute(
            select(w.spot_id, func.min(w.valid_time), func.max(w.valid_time))
            .where(w.run_id.in_(run_ids))
            .group_by(w.spot_id)
        )
    }


def compute_pending_predictions(db: Session, now: datetime | None = None) -> dict[str, int]:
    pending = db.scalars(
        select(ForecastRun.id)
        .where(
            ForecastRun.predictions_computed_at.is_(None),
            ForecastRun.status.in_((ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL)),
        )
        .order_by(ForecastRun.issued_at)
    ).all()
    total = 0
    reads: PassReads | None = None
    with shared_tide_reads(db, _scored_windows(db, pending)):
        for run_id in pending:
            reads = reads or PassReads.load(db)
            total += compute_predictions_for_run(db, run_id, now=now, shared=reads)
        rescored = outdated_authoritative_runs(db, now)
        for run_id in rescored:
            reads = reads or PassReads.load(db)
            total += compute_predictions_for_run(db, run_id, force=True, now=now, shared=reads)
    archived = 0
    # The prediction archive (verification and ML training data) is optional: off by
    # default, on in the full-retention profile (PREDICTION_ARCHIVE_ENABLED).
    if get_settings().prediction_archive_enabled:
        from app.services.forecasts.archive import archive_authoritative

        archived = archive_authoritative(db, now)["archived"]
    return {
        "runs": len(pending),
        "rescored_runs": len(rescored),
        "predictions": total,
        "archived": archived,
    }


def _row_values(f: ScoringForecast) -> dict[str, object]:
    """The forecast values the correction's features are built from (ml.features)."""
    return {
        name: getattr(f, name)
        for name in (
            "sig_wave_height_m",
            "wave_period_s",
            "wave_period_kind",
            "wave_direction_deg",
            "primary_swell_height_m",
            "primary_swell_period_s",
            "primary_swell_direction_deg",
            "wind_speed_kmh",
            "wind_direction_deg",
        )
    }


def _ml_correction(f: ScoringForecast) -> str | None:
    """Label of the correction a blend applied to one of this hour's members, if any."""
    applied = (f.extra or {}).get("ml_corrections") or {}
    labels = sorted({str(v.get("model")) for v in applied.values() if isinstance(v, dict)})
    return ", ".join(labels)[:60] or None
