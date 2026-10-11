"""Combine the latest run of each independent model family into one forecast.

Which sources take part
  The newest usable, non-stale deterministic run of every configured source. Sources of
  the same dependency family (``families.py``) are not independent evidence: the
  highest-priority one becomes the member and the others are kept only as an integrity
  cross-check (two copies of the same model should agree; when they do not, ingestion of
  one of them is suspect and the hour is flagged). A blend is built only when at least
  two families are present; otherwise the single best source stays authoritative.

How members are weighted (never a plain average)
  Each member i at valid time t has a lead L_i = t − issue time. Its log-space error σ_i
  is taken from verification of that family when available (``forecast_accuracy_metrics``
  scaled by the lead-time curve), otherwise from the documented lead-time prior
  ``uncertainty.lead_sigma(L_i)`` — so an older run automatically counts less. Errors of
  different families are correlated (shared observations, related forcing); the prior
  correlation is ``families.prior_error_correlation``. The weights are the best linear
  unbiased combination for correlated errors, w = Σ⁻¹·1 / (1ᵀ·Σ⁻¹·1) with
  Σ_ij = ρ_ij·σ_i·σ_j, clipped at zero and renormalised.

What is blended
  * Hs: weighted geometric mean (errors are close to log-normal);
  * Tp: weighted mean over members whose period is a peak period; otherwise the
    structure member's period;
  * swell/wind-sea partitions: taken from the highest-priority member that has them
    and scaled by Hs_blend / Hs_member (energy-consistent; directions and periods kept).
    Partition indices are height ranks, so partitions of different models are never
    matched index by index;
  * wave direction: from the structure member (GFS-Wave publishes the peak direction,
    ECMWF the mean direction — different quantities, compared but not averaged);
  * wind: speed and gust as weighted means, direction as the weighted vector mean of unit
    vectors; pressure as a weighted mean; sea level from the first member that has it.
  Members are aligned in time by linear interpolation between their own steps (circular
  for directions) across gaps of at most MAX_INTERP_GAP; never extrapolated.

Disagreement
  For every pair of independent families: |ln(Hs_a/Hs_b)|, |ΔTp| (peak periods only),
  angular Δ direction and |Δ wind speed|. Thresholds (heuristic, documented) raise flags;
  ``agreement`` in [0, 1] summarises them. The uncertainty model widens the forecast range
  by the between-model spread (``predict._between_model_sigma``).
"""

from __future__ import annotations

import bisect
import hashlib
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import (
    ForecastAccuracyMetric,
    ForecastComparison,
    ForecastRun,
    ForecastSource,
    WaveForecast,
)
from app.models.enums import ForecastRunStatus
from app.services.forecasts.base import FORECAST_FIELDS, ForecastRecord, SourceInfo
from app.services.forecasts.families import are_independent, prior_error_correlation
from app.services.forecasts.ingestion import AcquisitionOutcome, store_records
from app.services.forecasts.queries import USABLE, is_stale
from app.services.forecasts.registry import configured_source_codes
from app.services.ml.hook import RunCorrector, scale_heights, spot_slugs
from app.services.surf_quality.uncertainty import lead_sigma

logger = logging.getLogger(__name__)

BLEND_CODE = "blend"
METHOD = "blue_correlated_v1"
MAX_INTERP_GAP = timedelta(hours=6)
INSERT_CHUNK = 2000

# Disagreement thresholds between independent families (heuristic; docs/FORECAST_SOURCES.md).
HS_LOG_RATIO_FLAG = math.log(1.4)  # > 40 % apart
TP_DIFF_FLAG_S = 3.0
DIRECTION_DIFF_FLAG_DEG = 30.0
DIRECTION_DIFF_FLAG_MIXED_DEG = 45.0  # peak vs mean direction legitimately differ more
MEAN_DIRECTION_FAMILIES = frozenset({"ecmwf_ifs", "ecmwf_aifs"})
WIND_DIFF_FLAG_KMH = 15.0
# Two copies of the same model should be close; beyond this one ingestion is suspect.
SAME_FAMILY_LOG_RATIO_FLAG = math.log(1.2)
# Scales of the agreement summary (heuristic).
AGREEMENT_HS_SCALE = 0.25
AGREEMENT_WIND_SCALE_KMH = 12.0

PARTITION_HEIGHTS = (
    "primary_swell_height_m",
    "secondary_swell_height_m",
    "tertiary_swell_height_m",
    "wind_wave_height_m",
)
PARTITION_FIELDS = tuple(
    f"{prefix}_{suffix}"
    for prefix in ("primary_swell", "secondary_swell", "tertiary_swell", "wind_wave")
    for suffix in ("height_m", "period_s", "direction_deg")
)
DIRECTION_FIELDS = {f for f in FORECAST_FIELDS if f.endswith("direction_deg")}

# Point-forecast sources whose verification describes each family (see verification).
FAMILY_VERIFICATION_SOURCES: dict[str, tuple[str, ...]] = {
    "ncep_gfs": ("gfswave_bulletin", "gfswave_grid"),
    "ecmwf_ifs": ("ecmwf_ifs_wave",),
    "ncep_gefs": ("gefs_wave_mean",),
}


def blend_source_info() -> SourceInfo:
    return SourceInfo(
        code=BLEND_CODE,
        name="Swell Travel Agent multi-model blend",
        provider="swell_travel_agent",
        wave_model="blend of independent wave models",
        atmosphere_model="blend of independent atmospheric models",
        is_demo=False,
        attribution="Combination of the member sources listed on each run; their "
        "attributions and licences apply.",
        license_notes="Derived data; the member sources' licence terms apply.",
        family="blend",
        kind="blend",
    )


# --------------------------------------------------------------------------- members
@dataclass
class Member:
    source: ForecastSource
    run: ForecastRun
    series: dict[int, list[WaveForecast]] = field(default_factory=dict)

    @property
    def code(self) -> str:
        return self.source.code

    @property
    def family(self) -> str:
        return self.source.family


def _latest_usable_run(db: Session, source: ForecastSource) -> ForecastRun | None:
    return db.scalar(
        select(ForecastRun)
        .where(ForecastRun.source_id == source.id, ForecastRun.status.in_(USABLE))
        .order_by(ForecastRun.issued_at.desc(), ForecastRun.id.desc())
        .limit(1)
    )


def select_members(db: Session, now: datetime) -> tuple[list[Member], list[Member]]:
    """(one member per family in priority order, same-family duplicates)."""
    members: list[Member] = []
    duplicates: list[Member] = []
    seen: set[str] = set()
    for code in configured_source_codes():
        source = db.scalar(select(ForecastSource).where(ForecastSource.code == code))
        if source is None or source.is_demo or source.kind != "deterministic":
            continue
        run = _latest_usable_run(db, source)
        if run is None or is_stale(run, now):
            continue
        member = Member(source, run)
        if source.family in seen or source.family == "unknown":
            duplicates.append(member)
        else:
            seen.add(source.family)
            members.append(member)
    return members, duplicates


def _load_series(db: Session, member: Member) -> None:
    by_spot: dict[int, list[WaveForecast]] = {}
    for row in db.scalars(
        select(WaveForecast)
        .where(WaveForecast.run_id == member.run.id)
        .order_by(WaveForecast.spot_id, WaveForecast.valid_time)
    ):
        by_spot.setdefault(row.spot_id, []).append(row)
    member.series = by_spot


# --------------------------------------------------------------------------- alignment
def _interp_angle(a: float, b: float, frac: float) -> float:
    ax, ay = math.cos(math.radians(a)), math.sin(math.radians(a))
    bx, by = math.cos(math.radians(b)), math.sin(math.radians(b))
    x, y = ax + (bx - ax) * frac, ay + (by - ay) * frac
    if math.hypot(x, y) < 1e-9:
        return a if frac < 0.5 else b
    return math.degrees(math.atan2(y, x)) % 360.0


def value_at(
    rows: list[WaveForecast], times: list[datetime], t: datetime, name: str
) -> float | None:
    """Field ``name`` at time t, interpolating between neighbouring steps (no extrapolation)."""
    i = bisect.bisect_left(times, t)
    if i < len(times) and times[i] == t:
        return getattr(rows[i], name)
    if i == 0 or i == len(times):
        return None
    t0, t1 = times[i - 1], times[i]
    if t1 - t0 > MAX_INTERP_GAP:
        return None
    v0, v1 = getattr(rows[i - 1], name), getattr(rows[i], name)
    if v0 is None or v1 is None:
        return None
    frac = (t - t0) / (t1 - t0)
    if name in DIRECTION_FIELDS:
        return _interp_angle(v0, v1, frac)
    return float(v0 + (v1 - v0) * frac)


# --------------------------------------------------------------------------- weights
def correlated_weights(sigmas: list[float], families: list[str]) -> list[float]:
    """BLUE weights for estimators with correlated errors, clipped at zero."""
    n = len(sigmas)
    if n == 1:
        return [1.0]
    cov = np.empty((n, n))
    for i in range(n):
        for j in range(n):
            cov[i, j] = prior_error_correlation(families[i], families[j]) * sigmas[i] * sigmas[j]
    try:
        raw = np.linalg.solve(cov, np.ones(n))
    except np.linalg.LinAlgError:
        raw = 1.0 / np.square(np.asarray(sigmas))
    raw = np.clip(raw, 0.0, None)
    total = float(raw.sum())
    if total <= 0:
        return [1.0 / n] * n
    return [float(w) / total for w in raw]


@dataclass
class SkillTable:
    """Relative error scale per family from verification (1.0 = no evidence)."""

    scale: dict[str, float] = field(default_factory=dict)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)

    def factor(self, family: str) -> float:
        return self.scale.get(family, 1.0)


MIN_VERIFICATION_PAIRS = 200


def load_skill(db: Session, families: list[str]) -> SkillTable:
    """Relative RMSE of each family's Hs over the same scope, from forecast_accuracy_metrics.

    Used only when every member family has global verification with enough pairs; the
    ratios to their mean then scale the lead-time prior. Otherwise all factors are 1.
    """
    rmse: dict[str, float] = {}
    evidence: dict[str, dict[str, Any]] = {}
    for fam in families:
        codes = FAMILY_VERIFICATION_SOURCES.get(fam, ())
        if not codes:
            return SkillTable()
        rows = db.scalars(
            select(ForecastAccuracyMetric).where(
                ForecastAccuracyMetric.source_code.in_(codes),
                ForecastAccuracyMetric.model_version == "raw",
                ForecastAccuracyMetric.variable == "hs",
                ForecastAccuracyMetric.scope == "global",
            )
        ).all()
        usable = [r for r in rows if r.n >= MIN_VERIFICATION_PAIRS and r.rmse]
        if not usable:
            return SkillTable()
        n = sum(r.n for r in usable)
        rmse[fam] = math.sqrt(sum(r.n * (r.rmse or 0.0) ** 2 for r in usable) / n)
        evidence[fam] = {"pairs": n, "rmse_m": round(rmse[fam], 3)}
    mean = sum(rmse.values()) / len(rmse)
    return SkillTable({f: v / mean for f, v in rmse.items()}, evidence)


# --------------------------------------------------------------------------- blending
@dataclass
class HourValues:
    code: str
    family: str
    lead_hours: float
    values: dict[str, float | None]
    period_kind: str | None


def blend_hour(
    hours: list[HourValues], skill: SkillTable
) -> tuple[dict[str, float | None], dict[str, float], str | None]:
    """(blended values, weight per source code, period kind) for one spot and hour."""
    with_hs = [h for h in hours if (h.values.get("sig_wave_height_m") or 0) > 0]
    weights: dict[str, float] = {}
    if with_hs:
        sig = [lead_sigma(h.lead_hours) * skill.factor(h.family) for h in with_hs]
        for h, w in zip(with_hs, correlated_weights(sig, [h.family for h in with_hs]), strict=True):
            weights[h.code] = w

    def wmean(name: str, members: list[HourValues]) -> float | None:
        total = acc = 0.0
        for h in members:
            w, v = weights.get(h.code, 0.0), h.values.get(name)
            if v is not None and w > 0:
                total += w
                acc += w * v
        return acc / total if total > 0 else None

    out: dict[str, float | None] = {k: None for k in FORECAST_FIELDS}
    structure = next(
        (h for h in hours if h.values.get("primary_swell_height_m") is not None), hours[0]
    )
    hs_blend: float | None = None
    if with_hs:
        log_hs = 0.0
        for h in with_hs:
            hs = h.values.get("sig_wave_height_m")
            if hs:
                log_hs += weights[h.code] * math.log(hs)
        hs_blend = math.exp(log_hs)
    out["sig_wave_height_m"] = hs_blend

    peak = [h for h in hours if h.period_kind == "tp"]
    period_kind = structure.period_kind
    if peak and all(weights.get(h.code, 0) > 0 for h in peak):
        out["wave_period_s"] = wmean("wave_period_s", peak)
        period_kind = "tp"
    else:
        out["wave_period_s"] = structure.values.get("wave_period_s")
    out["wave_direction_deg"] = structure.values.get("wave_direction_deg")

    hs_struct = structure.values.get("sig_wave_height_m")
    ratio = hs_blend / hs_struct if hs_blend and hs_struct else 1.0
    for name in PARTITION_FIELDS:
        v = structure.values.get(name)
        out[name] = v * ratio if v is not None and name in PARTITION_HEIGHTS else v

    out["wind_speed_kmh"] = wmean("wind_speed_kmh", hours)
    out["wind_gust_kmh"] = wmean("wind_gust_kmh", hours)
    out["pressure_msl_hpa"] = wmean("pressure_msl_hpa", hours)
    vx = vy = 0.0
    for h in hours:
        d, w = h.values.get("wind_direction_deg"), weights.get(h.code, 0.0)
        if d is not None and w > 0:
            vx += w * math.sin(math.radians(d))
            vy += w * math.cos(math.radians(d))
    out["wind_direction_deg"] = (
        math.degrees(math.atan2(vx, vy)) % 360.0 if math.hypot(vx, vy) > 1e-9 else None
    )
    out["sea_level_m"] = next(
        (h.values["sea_level_m"] for h in hours if h.values.get("sea_level_m") is not None), None
    )
    return out, weights, period_kind


def angular_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def compare_hour(hours: list[HourValues], duplicates: list[HourValues]) -> dict[str, Any]:
    """Pairwise disagreement between independent families, plus same-family checks."""
    hs_r = tp_d = dir_d = wind_d = None
    flags: list[str] = []
    direction_flag = False
    for i, a in enumerate(hours):
        for b in hours[i + 1 :]:
            if not are_independent(a.family, b.family):
                continue
            ha, hb = a.values.get("sig_wave_height_m"), b.values.get("sig_wave_height_m")
            if ha and hb and ha > 0 and hb > 0:
                r = abs(math.log(ha / hb))
                hs_r = r if hs_r is None else max(hs_r, r)
            if a.period_kind == b.period_kind == "tp":
                ta, tb = a.values.get("wave_period_s"), b.values.get("wave_period_s")
                if ta is not None and tb is not None:
                    tp_d = max(tp_d or 0.0, abs(ta - tb))
            da, db = a.values.get("wave_direction_deg"), b.values.get("wave_direction_deg")
            if da is not None and db is not None:
                d = angular_diff(da, db)
                dir_d = d if dir_d is None else max(dir_d, d)
                # ECMWF publishes the mean direction, the NOAA chain the peak direction.
                mixed = (a.family in MEAN_DIRECTION_FAMILIES) != (
                    b.family in MEAN_DIRECTION_FAMILIES
                )
                limit = DIRECTION_DIFF_FLAG_MIXED_DEG if mixed else DIRECTION_DIFF_FLAG_DEG
                direction_flag = direction_flag or d > limit
            wa, wb = a.values.get("wind_speed_kmh"), b.values.get("wind_speed_kmh")
            if wa is not None and wb is not None:
                wind_d = max(wind_d or 0.0, abs(wa - wb))
    if hs_r is not None and hs_r > HS_LOG_RATIO_FLAG:
        flags.append("hs_disagree")
    if tp_d is not None and tp_d > TP_DIFF_FLAG_S:
        flags.append("period_disagree")
    if direction_flag:
        flags.append("direction_disagree")
    if wind_d is not None and wind_d > WIND_DIFF_FLAG_KMH:
        flags.append("wind_disagree")
    by_family = {h.family: h for h in hours}
    for dup in duplicates:
        ref = by_family.get(dup.family)
        hd = dup.values.get("sig_wave_height_m")
        hr = ref.values.get("sig_wave_height_m") if ref else None
        if hd and hr and hd > 0 and hr > 0 and abs(math.log(hd / hr)) > SAME_FAMILY_LOG_RATIO_FLAG:
            flags.append(f"same_family_mismatch:{dup.code}")
    agreement = None
    if hs_r is not None:
        agreement = 0.7 * math.exp(-0.5 * (hs_r / AGREEMENT_HS_SCALE) ** 2)
        agreement += 0.3 * (
            math.exp(-0.5 * (wind_d / AGREEMENT_WIND_SCALE_KMH) ** 2) if wind_d is not None else 1.0
        )
    return {
        "hs_log_ratio_max": hs_r,
        "tp_diff_max_s": tp_d,
        "direction_diff_max_deg": dir_d,
        "wind_diff_max_kmh": wind_d,
        "agreement": None if agreement is None else round(agreement, 3),
        "flags": flags,
    }


def _run_key(members: list[Member], duplicates: list[Member]) -> str:
    parts = "|".join(f"{m.code}@{m.run.run_key}" for m in [*members, *duplicates])
    return f"blend:{hashlib.sha1(parts.encode()).hexdigest()[:16]}"  # noqa: S324 - identifier only


def _ensure_blend_source(db: Session) -> ForecastSource:
    info = blend_source_info()
    source = db.scalar(select(ForecastSource).where(ForecastSource.code == info.code))
    if source is None:
        source = ForecastSource(code=info.code)
        db.add(source)
    for attr in ("name", "provider", "wave_model", "atmosphere_model", "is_demo", "family", "kind"):
        setattr(source, attr, getattr(info, attr))
    source.attribution = info.attribution
    source.license_notes = info.license_notes
    source.priority = -1
    source.is_enabled = True
    db.flush()
    return source


def build_blend(db: Session, now: datetime | None = None) -> AcquisitionOutcome:
    """Create the blend run for the current member runs (idempotent)."""
    now = now or utcnow()
    if not get_settings().forecast_blend_enabled:
        return AcquisitionOutcome(BLEND_CODE, None, "skipped", message="blending disabled")
    members, duplicates = select_members(db, now)
    if len(members) < 2:
        return AcquisitionOutcome(
            BLEND_CODE,
            None,
            "skipped",
            message=f"{len(members)} independent model famil{'y' if len(members) == 1 else 'ies'}",
        )
    key = _run_key(members, duplicates)
    source = _ensure_blend_source(db)
    existing = db.scalar(
        select(ForecastRun).where(ForecastRun.source_id == source.id, ForecastRun.run_key == key)
    )
    if existing is not None and existing.status in USABLE:
        return AcquisitionOutcome(BLEND_CODE, key, "skipped", existing.id, message="already built")
    run = existing or ForecastRun(source_id=source.id, run_key=key, details={})
    if existing is None:
        db.add(run)
    run.issued_at = min(m.run.issued_at for m in members)  # conservative lead/staleness
    run.wave_model_run_at = run.atmosphere_model_run_at = None
    run.started_at = now
    run.status = ForecastRunStatus.RUNNING
    run.predictions_computed_at = None
    db.flush()

    for m in [*members, *duplicates]:
        _load_series(db, m)
    skill = load_skill(db, [m.family for m in members])
    # An active ML correction is applied to the members of the family it was validated on,
    # before combining (ml.hook); cross-checks stay raw so integrity checks compare raw data.
    correctors = {m.code: c for m in members if (c := RunCorrector.for_run(db, m.run)) is not None}
    slugs = spot_slugs(db) if correctors else {}
    applied_labels: set[str] = set()
    records: dict[int, list[ForecastRecord]] = {}
    comparisons: list[dict[str, Any]] = []
    spot_ids = sorted({s for m in members for s in m.series})
    for spot_id in spot_ids:
        aligned = [
            (m, m.series[spot_id], [r.valid_time for r in m.series[spot_id]])
            for m in [*members, *duplicates]
            if spot_id in m.series
        ]
        anchor = next((a for a in aligned if a[0] in members), None)
        if anchor is None:
            continue
        out: list[ForecastRecord] = []
        for t in anchor[2]:
            hours: list[HourValues] = []
            dup_hours: list[HourValues] = []
            corrected: dict[str, dict[str, Any]] = {}
            for m, rows, times in aligned:
                values = {name: value_at(rows, times, t, name) for name in FORECAST_FIELDS}
                if all(v is None for v in values.values()):
                    continue
                corrector = correctors.get(m.code) if m in members else None
                if corrector is not None:
                    row = {**values, "wave_period_kind": rows[0].wave_period_kind}
                    hit = corrector.factor(slugs.get(spot_id, ""), row, t)
                    if hit is not None:
                        values = scale_heights(values, hit[0])
                        corrected[m.code] = hit[1]
                        applied_labels.add(corrector.label)
                hv = HourValues(
                    m.code,
                    m.family,
                    (t - m.run.issued_at).total_seconds() / 3600.0,
                    values,
                    rows[0].wave_period_kind,
                )
                (hours if m in members else dup_hours).append(hv)
            if not hours:
                continue
            values, weights, kind = blend_hour(hours, skill)
            if values["sig_wave_height_m"] is None and values["primary_swell_height_m"] is None:
                continue
            extra: dict[str, Any] = {"families": len({h.family for h in hours})}
            if corrected:
                extra["ml_corrections"] = corrected
            out.append(ForecastRecord(t, values, period_kind=kind, extra=extra))
            cmp_ = compare_hour(hours, dup_hours)
            comparisons.append(
                {
                    "blend_run_id": run.id,
                    "spot_id": spot_id,
                    "valid_time": t,
                    "sources": {
                        h.code: {
                            "family": h.family,
                            "lead_hours": round(h.lead_hours, 1),
                            "hs": h.values.get("sig_wave_height_m"),
                            "tp": h.values.get("wave_period_s"),
                            "tp_kind": h.period_kind,
                            "dp": h.values.get("wave_direction_deg"),
                            "wind_kmh": h.values.get("wind_speed_kmh"),
                            "wind_dir": h.values.get("wind_direction_deg"),
                            "weight": round(weights.get(h.code, 0.0), 3),
                            "role": "member" if h in hours else "cross_check",
                        }
                        for h in [*hours, *dup_hours]
                    },
                    "independent_families": len({h.family for h in hours}),
                    "method": METHOD,
                    **cmp_,
                }
            )
        if out:
            records[spot_id] = out
    count = store_records(db, run, records)
    stmt = insert(ForecastComparison).on_conflict_do_nothing(
        index_elements=["blend_run_id", "spot_id", "valid_time"]
    )
    for i in range(0, len(comparisons), INSERT_CHUNK):
        db.execute(stmt, comparisons[i : i + INSERT_CHUNK])
    flagged = sum(1 for c in comparisons if c["flags"])
    run.record_count = count
    run.spot_count = len(records)
    run.details = {
        "metadata_source": "blend",
        "method": METHOD,
        "spot_ids": sorted(records),
        "members": [_member_info(m) for m in members],
        "ml_corrections": sorted(applied_labels),
        "cross_checks": [_member_info(m) for m in duplicates],
        "skill": skill.evidence or "no verification yet: lead-time prior only",
        "flagged_hours": flagged,
    }
    run.status = ForecastRunStatus.SUCCESS if records else ForecastRunStatus.FAILED
    if not records:
        run.error_message = "no overlapping member data"
    run.completed_at = utcnow()
    db.commit()
    logger.info(
        "Built blend %s from %s: %d records, %d flagged hours",
        key,
        ", ".join(m.code for m in members),
        count,
        flagged,
    )
    return AcquisitionOutcome(BLEND_CODE, key, run.status.value, run.id, records=count)


def _member_info(m: Member) -> dict[str, Any]:
    return {
        "code": m.code,
        "family": m.family,
        "run_id": m.run.id,
        "run_key": m.run.run_key,
        "issued_at": m.run.issued_at.isoformat(),
    }
