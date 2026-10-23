"""Database inputs of forecast-v1 confidence for one model run (read once per run).

``RunConfidenceContext.load`` gathers what ``forecast_confidence`` needs beyond the hour's
own values and uncertainty:

* for a blend run, every hour's ``forecast_comparisons`` row: the member families and the
  disagreement between INDEPENDENT families (the blend only measures those);
* the previous run of the same source: offshore Hs per spot and valid time (run
  consistency);
* verification rows (``forecast_accuracy_metrics``, Hs, raw model) of the families behind
  the run, for buoys paired with each spot (``spot_physical_profiles.reference_stations``)
  and globally, per lead bucket;
* the current research assessment of every spot (local confidence);
* whether the run is stale by its source's rule (``queries.is_stale``).

Only the columns and JSON keys these inputs use are read (database egress; a scoring pass
runs after every model run). Of a comparison's ``sources`` only the members' ``family``
values arrive, and an assessment's ``per_variable`` without the evidence lists of the
variables other than wind (the wind dispute ranks the wind evidence). Every value the
inputs use is the stored one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Text, and_, case, cast, column, func, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.models import (
    ForecastAccuracyMetric,
    ForecastComparison,
    ForecastRun,
    SpotResearchAssessment,
    WaveForecast,
)
from app.models.enums import ForecastRunStatus
from app.services.forecasts.blend import FAMILY_VERIFICATION_SOURCES, MIN_VERIFICATION_PAIRS
from app.services.forecasts.queries import BLEND_CODE, is_stale, stale_after_hours
from app.services.surf_quality.forecast_confidence import (
    Disagreement,
    ResearchBasisInput,
    Verification,
    research_input,
)
from app.services.verification.metrics import LEAD_BUCKETS_H

VERIFICATION_SCOPES = ("station", "global")


@dataclass(frozen=True)
class HourComparison:
    """One blended hour: member families, independent disagreement, between-model σ."""

    families: tuple[str, ...]
    disagreement: Disagreement
    sigma: float | None  # half the largest independent |ln(Hs_a / Hs_b)|


def nearest_lead_bucket(lead_hours: float) -> int:
    """The verification lead bucket closest to this lead (ties: the shorter bucket)."""
    return min(LEAD_BUCKETS_H, key=lambda b: (abs(b - lead_hours), b))


@dataclass
class VerificationIndex:
    """Newest qualifying metric row per (scope, scope_key, source_code, lead bucket)."""

    rows: dict[tuple[str, str, str, int], ForecastAccuracyMetric] = field(default_factory=dict)

    @classmethod
    def load(cls, db: Session, source_codes: set[str]) -> VerificationIndex:
        if not source_codes:
            return cls()
        rows = db.scalars(
            select(ForecastAccuracyMetric)
            .where(
                ForecastAccuracyMetric.source_code.in_(sorted(source_codes)),
                ForecastAccuracyMetric.variable == "hs",
                ForecastAccuracyMetric.model_version == "raw",
                ForecastAccuracyMetric.scope.in_(VERIFICATION_SCOPES),
                ForecastAccuracyMetric.lead_bucket_h.in_(LEAD_BUCKETS_H),
                ForecastAccuracyMetric.n >= MIN_VERIFICATION_PAIRS,
            )
            # newest window first; among windows ending together, the one with most pairs
            .order_by(ForecastAccuracyMetric.period_end.desc(), ForecastAccuracyMetric.n.desc())
        ).all()
        out: dict[tuple[str, str, str, int], ForecastAccuracyMetric] = {}
        for r in rows:
            out.setdefault((r.scope, r.scope_key, r.source_code, r.lead_bucket_h), r)
        return cls(out)

    def lookup(
        self, families: tuple[str, ...], station_codes: list[str], lead_hours: float
    ) -> tuple[Verification, ...]:
        """One row per family: a paired buoy's when one exists, else the global one."""
        bucket = nearest_lead_bucket(lead_hours)
        found: list[Verification] = []
        for fam in dict.fromkeys(families):
            choice: tuple[ForecastAccuracyMetric, bool] | None = None
            for code in FAMILY_VERIFICATION_SOURCES.get(fam, ()):
                station = [
                    r
                    for s in station_codes
                    if (r := self.rows.get(("station", s, code, bucket))) is not None
                ]
                if station:
                    choice = (max(station, key=lambda r: r.n), True)
                    break
                glob = self.rows.get(("global", "all", code, bucket))
                if glob is not None and choice is None:
                    choice = (glob, False)
            if choice is not None:
                row, near = choice
                found.append(
                    Verification(
                        scope=row.scope,
                        scope_key=row.scope_key,
                        source_code=row.source_code,
                        lead_bucket_h=row.lead_bucket_h,
                        n=row.n,
                        scatter_index=row.scatter_index,
                        skill_vs_persistence=row.skill_vs_persistence,
                        near_spot=near,
                    )
                )
        return tuple(found)


def previous_run(db: Session, run: ForecastRun) -> ForecastRun | None:
    """The previous usable run of the same source (run consistency)."""
    return db.scalar(
        select(ForecastRun)
        .where(
            ForecastRun.source_id == run.source_id,
            ForecastRun.id != run.id,
            ForecastRun.issued_at <= run.issued_at,
            ForecastRun.status.in_((ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL)),
        )
        .order_by(ForecastRun.issued_at.desc(), ForecastRun.id.desc())
        .limit(1)
    )


def _member_families(sources: Any) -> ColumnElement[Any]:
    """The ``family`` value of every member of ``sources`` (code -> per-member values) whose
    ``role`` is "member" or absent, as a JSON array (in no particular order): all that
    :func:`_comparisons` reads of the column (Hs, Tp, direction, wind, weight and lead are
    about 9/10 of it). Which values count and their text stay in Python, as when the whole
    column was read. NULL when there is none, which reads as empty."""
    member = (
        func.jsonb_each(sources)
        .table_valued(column("key", Text), column("value", JSONB))
        .render_derived(name="m")
    )
    value = member.c.value
    role = value["role"]
    return (
        select(func.jsonb_agg(value["family"], type_=JSONB))
        .select_from(member)
        .where(
            func.jsonb_typeof(value) == "object",
            value.has_key("family"),
            or_(
                ~value.has_key("role"),
                and_(func.jsonb_typeof(role) == "string", role.astext == "member"),
            ),
        )
        .scalar_subquery()
    )


def _comparisons(db: Session, run: ForecastRun) -> dict[tuple[int, datetime], HourComparison]:
    out: dict[tuple[int, datetime], HourComparison] = {}
    if run.source.code != BLEND_CODE:
        return out
    # JSONB objects do not keep key order: list families in the run's member order.
    order = list(_run_families(run))
    c = ForecastComparison
    for row in db.execute(
        select(
            c.spot_id,
            c.valid_time,
            _member_families(c.sources).label("families"),
            c.hs_log_ratio_max,
            c.tp_diff_max_s,
            c.direction_diff_max_deg,
            c.wind_diff_max_kmh,
            c.flags,
        ).where(c.blend_run_id == run.id)
    ):
        families = tuple(
            sorted(
                {str(f) for f in row.families or () if f},
                key=lambda f: order.index(f) if f in order else len(order),
            )
        )
        out[(row.spot_id, row.valid_time)] = HourComparison(
            families=families,
            disagreement=Disagreement(
                hs_log_ratio=row.hs_log_ratio_max,
                tp_diff_s=row.tp_diff_max_s,
                direction_diff_deg=row.direction_diff_max_deg,
                wind_diff_kmh=row.wind_diff_max_kmh,
                flags=tuple(str(f) for f in row.flags or ()),
            ),
            sigma=None if row.hs_log_ratio_max is None else row.hs_log_ratio_max / 2.0,
        )
    return out


def _run_families(run: ForecastRun) -> tuple[str, ...]:
    if run.source.code == BLEND_CODE:
        return tuple(str(m["family"]) for m in run.details.get("members", []) if m.get("family"))
    return (run.source.family,)


@dataclass(frozen=True, slots=True)
class AssessmentInputs:
    """The columns of a current research assessment that scoring reads (``id``,
    ``spot_id``, ``overall_class`` and ``per_variable`` as :func:`scoring_assessments`
    reads it), for ``forecast_confidence.research_input`` and the tide research class."""

    id: int
    spot_id: int
    overall_class: str
    per_variable: dict[str, Any] | None


def _without_evidence(per_variable: Any) -> ColumnElement[Any]:
    """``per_variable`` without the ``evidence`` list of every variable except wind (whose
    evidence ``research.decisions.wind_dispute`` ranks); about 4/5 of the column is
    evidence. NULL for an empty object, which reads as empty."""
    entry = func.jsonb_each(per_variable).table_valued("key", "value").render_derived(name="v")
    trimmed = case(
        (
            (entry.c.key != "wind") & (func.jsonb_typeof(entry.c.value) == "object"),
            entry.c.value.op("-")(cast(literal("evidence"), Text)),
        ),
        else_=entry.c.value,
    )
    return cast(
        select(func.jsonb_object_agg(entry.c.key, trimmed)).select_from(entry).scalar_subquery(),
        JSONB,
    )


def scoring_assessments(db: Session) -> dict[int, AssessmentInputs]:
    """The current research assessment of every assessed spot (spot_id -> row), as
    ``research.current_assessments`` returns them but with only what scoring reads."""
    a = SpotResearchAssessment
    rows = db.execute(
        select(a.id, a.spot_id, a.overall_class, _without_evidence(a.per_variable)).where(
            a.is_current
        )
    )
    return {
        spot_id: AssessmentInputs(row_id, spot_id, overall, per_variable)
        for row_id, spot_id, overall, per_variable in rows
    }


def research_inputs(assessments: dict[int, AssessmentInputs]) -> dict[int, ResearchBasisInput]:
    """spot_id -> the research basis of local confidence, for the assessed spots."""
    research: dict[int, ResearchBasisInput] = {}
    for spot_id, row in assessments.items():
        entry = research_input(row)
        if entry is not None:
            research[spot_id] = entry
    return research


@dataclass
class RunConfidenceContext:
    run: ForecastRun
    stale: bool
    stale_after_hours: float | None
    families: tuple[str, ...]
    comparisons: dict[tuple[int, datetime], HourComparison]
    previous_run: ForecastRun | None
    # (spot_id, valid_time) -> (offshore Hs, primary swell height) of the previous run
    previous: dict[tuple[int, datetime], tuple[float | None, float | None]]
    verification: VerificationIndex
    research: dict[int, ResearchBasisInput]

    @classmethod
    def load(
        cls,
        db: Session,
        run: ForecastRun,
        now: datetime,
        research: dict[int, ResearchBasisInput] | None = None,
    ) -> RunConfidenceContext:
        """``research``: :func:`research_inputs`, when the caller has read them already (a
        scoring pass reads the assessments once for all its runs)."""
        prev = previous_run(db, run)
        previous: dict[tuple[int, datetime], tuple[float | None, float | None]] = {}
        if prev is not None:
            for spot_id, valid_time, hs, swell in db.execute(
                select(
                    WaveForecast.spot_id,
                    WaveForecast.valid_time,
                    WaveForecast.sig_wave_height_m,
                    WaveForecast.primary_swell_height_m,
                ).where(WaveForecast.run_id == prev.id)
            ):
                previous[(spot_id, valid_time)] = (hs, swell)
        families = _run_families(run)
        codes = {c for fam in families for c in FAMILY_VERIFICATION_SOURCES.get(fam, ())}
        if research is None:
            research = research_inputs(scoring_assessments(db))
        return cls(
            run=run,
            stale=is_stale(run, now),
            stale_after_hours=None
            if run.source.code == BLEND_CODE
            else stale_after_hours(run.source.code),
            families=families,
            comparisons=_comparisons(db, run),
            previous_run=prev,
            previous=previous,
            verification=VerificationIndex.load(db, codes),
            research=research,
        )

    def between_model_sigma(self, spot_id: int, valid_time: datetime) -> float | None:
        hour = self.comparisons.get((spot_id, valid_time))
        return hour.sigma if hour is not None else None

    def hour_families(self, spot_id: int, valid_time: datetime) -> tuple[str, ...]:
        hour = self.comparisons.get((spot_id, valid_time))
        return hour.families if hour is not None and hour.families else self.families

    def disagreement(self, spot_id: int, valid_time: datetime) -> Disagreement | None:
        hour = self.comparisons.get((spot_id, valid_time))
        return hour.disagreement if hour is not None else None


def station_codes(spot: Any) -> list[str]:
    """Observation stations paired with the spot (its physical profile), if any."""
    profile = getattr(spot, "physical_profile", None)
    if profile is None:
        return []
    return [str(s["code"]) for s in profile.reference_stations or [] if s.get("code")]
