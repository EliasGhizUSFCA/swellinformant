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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ForecastAccuracyMetric, ForecastComparison, ForecastRun, WaveForecast
from app.models.enums import ForecastRunStatus
from app.services.forecasts.blend import FAMILY_VERIFICATION_SOURCES, MIN_VERIFICATION_PAIRS
from app.services.forecasts.queries import BLEND_CODE, is_stale, stale_after_hours
from app.services.research import current_assessments
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


def _comparisons(db: Session, run: ForecastRun) -> dict[tuple[int, datetime], HourComparison]:
    out: dict[tuple[int, datetime], HourComparison] = {}
    if run.source.code != BLEND_CODE:
        return out
    # JSONB objects do not keep key order: list families in the run's member order.
    order = list(_run_families(run))
    for row in db.scalars(
        select(ForecastComparison).where(ForecastComparison.blend_run_id == run.id)
    ):
        families = tuple(
            sorted(
                {
                    str(v.get("family"))
                    for v in (row.sources or {}).values()
                    if isinstance(v, dict)
                    and v.get("role", "member") == "member"
                    and v.get("family")
                },
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
    def load(cls, db: Session, run: ForecastRun, now: datetime) -> RunConfidenceContext:
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
        research: dict[int, ResearchBasisInput] = {}
        for spot_id, row in current_assessments(db).items():
            entry = research_input(row)
            if entry is not None:
                research[spot_id] = entry
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
