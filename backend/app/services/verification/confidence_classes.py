"""Forecast-confidence classes of archived forecasts, scored against buoy observations.

For every forecast-observation pair the forecast-confidence class is computed with
``surf_quality.forecast_confidence.classify_forecast`` from the inputs that exist for an
archived station forecast (docs/ml-evaluation.md section 12.0):

* lead time, one deterministic family (``ncep_gfs``), not stale, not demo;
* the 0-100 index exactly as the prediction job computes it (``breaking_uncertainty``
  for the spread source, ``confidence_index`` with ``completeness``). Completeness counts
  the partition-1 height, period and direction of the archived forecast and counts wind
  as PRESENT: the app's NOAA source always carries GFS 10 m wind, the station bulletin
  never does, and counting it missing would cap every hour at Moderate for a reason the
  app never has. ``missing_swell`` = no partition-1 period or direction;
* no disagreement between families and no run-to-run change (the archive's previous
  cycle is 48 h older, not the 6 h of the app, so its change would overstate the rule);
* optional GEFS-Wave ensemble mean and spread at the same valid time;
* optional verification rows as the scheduled job would have produced them before the
  cycle (:func:`causal_verification`): the station's own raw-forecast pairs with a valid
  time in the ``window_days`` before the cycle time, per lead bucket, n >= 200, with the
  scatter index and the MSE skill against persistence of ``verification.jobs``.

Scores per class (and class x lead bucket): n, bias, MAE, median and 90th percentile of
|error| (m), RMSE (m) and the median of |ln(obs / model)|, with cycle-block bootstrap CIs
on RMSE and on the RMSE differences between consecutive classes. ``monotonicity`` applies
the pre-registered test: RMSE, median |error| and p90 |error| increase strictly
High < Moderate < Low, and both consecutive RMSE-difference CIs exclude zero.

Methods: ``forecast-v1`` is the current ``classify_forecast``. :data:`CANDIDATE_C2` is
the candidate the boundary scan found on TRAIN, "no High from a single model without an
ensemble": forecast-v1 plus one cap, ``single_source_no_ensemble`` (:func:`c2_applies`,
inside 3 days, fewer than two independent families, no ensemble spread, not demo; beyond
3 days forecast-v1 already caps such hours). Since the class is ``max(Low, min(index
class, caps) - downgrades)``, adding the cap to the rules that fired
(:func:`class_with_caps`) gives exactly the class ``classify_forecast`` would give with
that rule. The candidate was NOT adopted (docs/ml-evaluation.md section 12.3): having no
High class it cannot pass the pre-registered three-class test on TEST, and on TEST
forecast-v1's single-source High hours did have a lower RMSE than its Moderate hours.

:func:`boundary_scan` is the TRAIN-only search for a better High / Moderate boundary of
single-source hours (docs/ml-evaluation.md section 12.3).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd

from app.services.forecasts.blend import MIN_VERIFICATION_PAIRS
from app.services.surf_quality.confidence import completeness
from app.services.surf_quality.confidence_context import nearest_lead_bucket
from app.services.surf_quality.forecast_confidence import (
    HIGH,
    INDEX_HIGH,
    LEAD_SHORT_H,
    LOW,
    MODERATE,
    EnsembleSignal,
    ForecastConfidence,
    ForecastInputs,
    Verification,
    classify_forecast,
    independent_family_count,
    lower,
    rank,
)
from app.services.surf_quality.uncertainty import (
    EnsembleInfo,
    breaking_uncertainty,
    confidence_index,
)
from app.services.verification import metrics as vm
from app.services.verification.ranges import log_error

CLASSES = (HIGH, MODERATE, LOW)
_BY_RANK = {rank(c): c for c in CLASSES}
# The class rules validated here are forecast-v1's, on one model family (FAMILIES).
# forecast-v1.1 adds only the disputed-wind note (no class rule) and the shipped
# forecast-v1.2 a cap that fires only with two independent families, so on this data both
# are the same rules.
METHOD = "forecast-v1"
SAME_CLASS_RULES = ("forecast-v1.1", "forecast-v1.2")
# The candidate of section 12.3 (not adopted): forecast-v1 plus the cap C2_RULE.
CANDIDATE_C2 = "candidate-c2"
METHODS = (METHOD, CANDIDATE_C2)
C2_RULE = "single_source_no_ensemble"
FAMILIES = ("ncep_gfs",)
SOURCE_CODE = "noaa_gfs"
NOAA_STALE_AFTER_H = 16.0
WINDOW_DAYS = 90
CONFIDENCE = 0.90
# Wind is counted as present (see the module docstring); any finite value does.
_WIND_PRESENT = 0.0


@dataclass(frozen=True)
class HourInputs:
    lead_hours: float
    swell_hs: float | None
    swell_tp: float | None
    swell_dir: float | None
    ens_mean: float | None = None
    ens_spread: float | None = None
    verification: Verification | None = None


def forecast_inputs(h: HourInputs) -> ForecastInputs:
    """ForecastInputs of one archived hour, built as in ``surf_quality.predict``."""
    ensemble = (
        EnsembleInfo(hs_mean_m=h.ens_mean, hs_spread_m=h.ens_spread)
        if h.ens_mean is not None and h.ens_spread is not None
        else None
    )
    unc = breaking_uncertainty(1.0, h.lead_hours, ensemble=ensemble)
    values = {
        "primary_swell_height_m": h.swell_hs,
        "primary_swell_period_s": h.swell_tp,
        "primary_swell_direction_deg": h.swell_dir,
        "wind_speed_kmh": _WIND_PRESENT,
        "wind_direction_deg": _WIND_PRESENT,
    }
    index = confidence_index(unc.sigma_forecast, completeness(values), stale=False)
    return ForecastInputs(
        index=index,
        sigma_source=unc.source,
        lead_hours=h.lead_hours,
        source_code=SOURCE_CODE,
        is_demo=False,
        families=FAMILIES,
        ensemble=None
        if ensemble is None
        else EnsembleSignal(ensemble.hs_mean_m, ensemble.hs_spread_m, None),
        disagreement=None,
        run_change_ratio=None,
        verification=() if h.verification is None else (h.verification,),
        missing_wind=False,
        missing_swell=h.swell_tp is None or h.swell_dir is None,
        stale=False,
        stale_after_hours=NOAA_STALE_AFTER_H,
    )


def class_with_caps(fc: ForecastConfidence, caps: Sequence[str] = ()) -> str:
    """The class ``classify_forecast`` gives when the extra ``caps`` (class names) fire
    besides the rules that did: max(Low, min(index class, every cap) - downgrades)."""
    cls = fc.index_class
    for r in fc.rules:
        if r.effect.startswith("cap:"):
            cls = lower(cls, r.effect.split(":", 1)[1])
    for to in caps:
        cls = lower(cls, to)
    downgrades = sum(1 for r in fc.rules if r.effect == "downgrade")
    return _BY_RANK[max(0, rank(cls) - downgrades)]


def c2_applies(inp: ForecastInputs) -> bool:
    """Would the candidate cap C2_RULE fire: lead <= 3 days, fewer than two independent
    families, no usable ensemble spread (none, or a flat-sea mean), not demo."""
    rel = inp.ensemble.rel_spread if inp.ensemble is not None else None
    return (
        inp.lead_hours <= LEAD_SHORT_H
        and rel is None
        and independent_family_count(inp.families) < 2
        and not inp.is_demo
    )


@lru_cache(maxsize=65536)
def _classify_cached(h: HourInputs, method: str) -> tuple[str, tuple[str, ...]]:
    inp = forecast_inputs(h)
    fc = classify_forecast(inp)
    fired = tuple(r.as_str() for r in fc.rules if r.effect != "note")
    if method == METHOD or method in SAME_CLASS_RULES:
        return fc.cls, fired
    if method == CANDIDATE_C2:
        if not c2_applies(inp):
            return fc.cls, fired
        return class_with_caps(fc, (MODERATE,)), (*fired, f"cap:{MODERATE}:{C2_RULE}")
    raise ValueError(f"unknown method {method!r}; one of {METHODS}")


def _opt(value: Any) -> float | None:
    if value is None:
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def classify_frame(
    frame: pd.DataFrame,
    verification: dict[tuple[str, pd.Timestamp, int], Verification] | None = None,
    *,
    ensemble: bool = False,
    method: str = METHOD,
) -> tuple[np.ndarray, np.ndarray]:
    """(class, rules that capped or downgraded) for every row of a dataset-style frame.

    Needs lead_hours, swell1_hs_m, swell1_tp_s and the swell1 direction (sin/cos), and
    with ``ensemble`` the columns ens_mean_m / ens_spread_m. ``verification`` maps
    (station_code, cycle_time, lead bucket) to the row the job would have produced.
    ``method``: one of :data:`METHODS`.
    """
    if method in SAME_CLASS_RULES:
        method = METHOD
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}; one of {METHODS}")
    lead = frame["lead_hours"].to_numpy(dtype=float)
    hs = frame["swell1_hs_m"].to_numpy(dtype=float)
    tp = frame["swell1_tp_s"].to_numpy(dtype=float)
    # Only presence matters for completeness / missing_swell: sin is NaN iff the direction is.
    dir_present = np.isfinite(frame["swell1_dir_sin"].to_numpy(dtype=float))
    mean = frame["ens_mean_m"].to_numpy(dtype=float) if ensemble else None
    spread = frame["ens_spread_m"].to_numpy(dtype=float) if ensemble else None
    codes = frame["station_code"].to_numpy()
    cycles = pd.to_datetime(frame["cycle_time"], utc=True)
    classes = np.empty(len(frame), dtype=object)
    rules = np.empty(len(frame), dtype=object)
    for i in range(len(frame)):
        ver = None
        if verification is not None:
            ver = verification.get((str(codes[i]), cycles.iloc[i], nearest_lead_bucket(lead[i])))
        h = HourInputs(
            lead_hours=round(float(lead[i]), 3),
            # Presence is all that completeness and missing_swell read (cache-friendly).
            swell_hs=0.0 if math.isfinite(hs[i]) else None,
            swell_tp=0.0 if math.isfinite(tp[i]) else None,
            swell_dir=0.0 if dir_present[i] else None,
            ens_mean=None if mean is None else _opt(round(float(mean[i]), 3)),
            ens_spread=None if spread is None else _opt(round(float(spread[i]), 3)),
            verification=ver,
        )
        classes[i], rules[i] = _classify_cached(h, method)
    return classes, rules


# ------------------------------------------------------------------------------ verification
def causal_verification(
    pairs: pd.DataFrame,
    cycles: pd.DataFrame,
    *,
    window_days: int = WINDOW_DAYS,
    min_pairs: int = MIN_VERIFICATION_PAIRS,
    source_code: str = "gfswave_bulletin",
) -> dict[tuple[str, pd.Timestamp, int], Verification]:
    """Verification row per (station, cycle, lead bucket) from pairs BEFORE the cycle.

    ``pairs``: every raw pair of the stations (station_code, cycle_time, valid_time,
    lead_hours, model_hs_m, obs_hs_m, persistence_hs_m). ``cycles``: the (station_code,
    cycle_time) to classify. For each, the pairs with valid time in [cycle - window,
    cycle) and a lead in the bucket give n, the scatter index and the MSE skill against
    persistence (only with >= MIN_REFERENCE_PAIRS persistence rows), as the scheduled job
    computes them; rows with n < ``min_pairs`` are left out (the app ignores them).
    """
    out: dict[tuple[str, pd.Timestamp, int], Verification] = {}
    window = pd.Timedelta(days=window_days)
    for code, group in pairs.groupby("station_code"):
        wanted = cycles[cycles["station_code"] == code]["cycle_time"]
        if wanted.empty:
            continue
        targets = pd.to_datetime(pd.Series(sorted(set(wanted))), utc=True)
        for bucket in vm.LEAD_BUCKETS_H:
            sub = group[vm.in_lead_bucket(group["lead_hours"].to_numpy(dtype=float), bucket)]
            sub = sub.sort_values("valid_time", kind="stable")
            vt = pd.to_datetime(sub["valid_time"], utc=True).astype("int64").to_numpy()
            m = sub["model_hs_m"].to_numpy(dtype=float)
            o = sub["obs_hs_m"].to_numpy(dtype=float)
            p = sub["persistence_hs_m"].to_numpy(dtype=float)
            ends = targets.astype("int64").to_numpy()
            starts = (targets - window).astype("int64").to_numpy()
            lo = np.searchsorted(vt, starts, side="left")
            hi = np.searchsorted(vt, ends, side="left")  # valid time < cycle time
            for cycle, a, b in zip(targets, lo, hi, strict=True):
                mm, oo, pp = m[a:b], o[a:b], p[a:b]
                n = vm.n_pairs(mm, oo)
                if n < min_pairs:
                    continue
                n_ref = int(vm.finite_pairs(mm, oo, pp)[0].size)
                ss = vm.skill_score(mm, oo, pp) if n_ref >= vm.MIN_REFERENCE_PAIRS else None
                out[(str(code), cycle, bucket)] = Verification(
                    scope="station",
                    scope_key=str(code),
                    source_code=source_code,
                    lead_bucket_h=bucket,
                    n=int(n),
                    scatter_index=vm.finite_or_none(vm.scatter_index(mm, oo)),
                    skill_vs_persistence=vm.finite_or_none(ss),
                    near_spot=True,
                )
    return out


# ------------------------------------------------------------------------------ scores
# Forecasts and observations are given to 0.01 m, so |error| takes discrete values; it is
# rounded to this resolution so that equal medians compare equal (0.25 - 0.2499999999...
# is floating-point noise, not a difference) in the strict monotonicity test.
ERROR_RESOLUTION_DECIMALS = 6


def _abs_err(m: np.ndarray, o: np.ndarray) -> np.ndarray:
    mm, oo = vm.finite_pairs(m, o)
    return np.round(np.abs(mm - oo), ERROR_RESOLUTION_DECIMALS)


def _rounded(value: Any) -> float:
    return round(float(value), ERROR_RESOLUTION_DECIMALS)


def class_scores(
    frame: pd.DataFrame, classes: np.ndarray, *, n_boot: int, seed: int
) -> dict[str, dict[str, Any]]:
    m = frame["model_hs_m"].to_numpy(dtype=float)
    o = frame["obs_hs_m"].to_numpy(dtype=float)
    le = np.abs(log_error(o, m))
    blocks = pd.to_datetime(frame["cycle_time"], utc=True).astype("int64").to_numpy()
    kw: dict[str, Any] = {"n_boot": n_boot, "confidence": CONFIDENCE, "seed": seed}
    out: dict[str, dict[str, Any]] = {}
    for cls in CLASSES:
        mask = classes == cls
        if not mask.any():
            out[cls] = {"n": 0}
            continue
        err = _abs_err(m[mask], o[mask])
        rmse_ci = vm.rmse_ci(m[mask], o[mask], blocks[mask], **kw)
        out[cls] = {
            "n": int(err.size),
            "cycles": int(np.unique(blocks[mask]).size),
            "bias": vm.finite_or_none(vm.bias(m[mask], o[mask])),
            "mae": vm.finite_or_none(float(np.mean(err))),
            "median_abs_error": vm.finite_or_none(_rounded(np.median(err))),
            "p90_abs_error": vm.finite_or_none(_rounded(np.quantile(err, 0.9))),
            "rmse": vm.finite_or_none(rmse_ci.estimate),
            "rmse_ci": [vm.finite_or_none(rmse_ci.lower), vm.finite_or_none(rmse_ci.upper)],
            "median_abs_log_error": vm.finite_or_none(float(np.nanmedian(le[mask]))),
            "mean_obs_hs": vm.finite_or_none(float(np.nanmean(o[mask]))),
        }
    return out


def rmse_gap_ci(
    frame: pd.DataFrame, classes: np.ndarray, lower: str, upper: str, *, n_boot: int, seed: int
) -> dict[str, Any]:
    """CI of RMSE(upper class) - RMSE(lower class), resampling whole cycles."""
    m = frame["model_hs_m"].to_numpy(dtype=float)
    o = frame["obs_hs_m"].to_numpy(dtype=float)
    blocks = pd.to_datetime(frame["cycle_time"], utc=True).astype("int64").to_numpy()
    a = (classes == lower).astype(float)
    b = (classes == upper).astype(float)
    if not a.any() or not b.any():
        return {"estimate": None, "ci": [None, None]}

    def stat(mm: np.ndarray, oo: np.ndarray, aa: np.ndarray, bb: np.ndarray) -> float:
        sa, sb = aa > 0, bb > 0
        if not sa.any() or not sb.any():
            return math.nan
        return vm.rmse(mm[sb], oo[sb]) - vm.rmse(mm[sa], oo[sa])

    ci = vm.cycle_block_bootstrap(
        stat, blocks, m, o, a, b, n_boot=n_boot, confidence=CONFIDENCE, seed=seed
    )
    return {
        "estimate": vm.finite_or_none(ci.estimate),
        "ci": [vm.finite_or_none(ci.lower), vm.finite_or_none(ci.upper)],
    }


def monotonicity(
    scores: dict[str, dict[str, Any]], gaps: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """The pre-registered test (module docstring); classes with no rows fail it."""
    checks: dict[str, bool] = {}
    for metric in ("rmse", "median_abs_error", "p90_abs_error"):
        vals = [scores.get(c, {}).get(metric) for c in CLASSES]
        checks[f"{metric}_increasing"] = all(v is not None for v in vals) and bool(
            vals[0] < vals[1] < vals[2]  # type: ignore[operator]
        )
    for name, gap in gaps.items():
        lo, hi = gap["ci"]
        checks[f"{name}_ci_excludes_zero"] = lo is not None and hi is not None and lo > 0
    return {"checks": checks, "passed": all(checks.values())}


def evaluate_classes(
    frame: pd.DataFrame,
    classes: np.ndarray,
    *,
    n_boot: int = vm.DEFAULT_N_BOOT,
    seed: int = vm.DEFAULT_BOOTSTRAP_SEED,
    buckets: Sequence[int] = vm.LEAD_BUCKETS_H,
) -> dict[str, Any]:
    scores = class_scores(frame, classes, n_boot=n_boot, seed=seed)
    gaps = {
        "moderate_minus_high": rmse_gap_ci(
            frame, classes, HIGH, MODERATE, n_boot=n_boot, seed=seed
        ),
        "low_minus_moderate": rmse_gap_ci(frame, classes, MODERATE, LOW, n_boot=n_boot, seed=seed),
    }
    lead = frame["lead_hours"].to_numpy(dtype=float)
    by_bucket: dict[str, dict[str, Any]] = {}
    for b in buckets:
        mask = vm.in_lead_bucket(lead, b)
        if not mask.any():
            continue
        sub = frame[mask].reset_index(drop=True)
        by_bucket[str(b)] = class_scores(sub, classes[mask], n_boot=n_boot, seed=seed)
    counts = {c: int((classes == c).sum()) for c in CLASSES}
    out = {
        "counts": counts,
        "scores": scores,
        "rmse_gaps": gaps,
        "monotonicity": monotonicity(scores, gaps),
        "by_bucket": by_bucket,
    }
    if counts[HIGH] == 0 and counts[MODERATE] and counts[LOW]:
        # No High at all (e.g. candidate C2 without an ensemble): the same test on the two
        # classes that exist, reported next to the (then failing) three-class test. It is a
        # diagnostic: the pre-registered test is the three-class one.
        out["monotonicity_present_classes"] = moderate_low_monotonicity(
            scores, gaps["low_minus_moderate"]
        )
    return out


def moderate_low_monotonicity(
    scores: dict[str, dict[str, Any]], gap: dict[str, Any]
) -> dict[str, Any]:
    """The pre-registered test restricted to Moderate < Low (diagnostic, not the
    pre-registered decision: that is the three-class test of :func:`monotonicity`)."""
    m, lo = scores.get(MODERATE, {}), scores.get(LOW, {})
    checks: dict[str, bool] = {}
    for metric in ("rmse", "median_abs_error", "p90_abs_error"):
        a, b = m.get(metric), lo.get(metric)
        checks[f"{metric}_increasing"] = a is not None and b is not None and bool(a < b)
    checks["low_minus_moderate_ci_excludes_zero"] = gap["ci"][0] is not None and gap["ci"][0] > 0
    return {"checks": checks, "passed": all(checks.values())}


def rule_effect_by_bucket(
    frame: pd.DataFrame, rules: np.ndarray, rule: str, *, n_boot: int, seed: int
) -> dict[str, Any]:
    """Per lead bucket: scores of hours where ``rule`` fired vs where it did not."""
    fired = np.array([any(rule in r for r in rs) for rs in rules], dtype=bool)
    lead = frame["lead_hours"].to_numpy(dtype=float)
    out: dict[str, Any] = {}
    for b in vm.LEAD_BUCKETS_H:
        mask = vm.in_lead_bucket(lead, b)
        if not mask.any():
            continue
        labels = np.where(fired[mask], MODERATE, HIGH)  # reuse class_scores' two slots
        sc = class_scores(frame[mask].reset_index(drop=True), labels, n_boot=n_boot, seed=seed)
        out[str(b)] = {"fired": sc[MODERATE], "not_fired": sc[HIGH]}
    return out


def window_cycles(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[["station_code", "cycle_time"]].drop_duplicates().reset_index(drop=True)


# ------------------------------------------------------------------------------ boundary scan
# Candidate High / Moderate boundaries (lead, h) for single-source hours inside 3 days.
SCAN_BOUNDARIES_H = (24.0, 30.0, 36.0, 42.0, 48.0, 54.0, 60.0, 66.0, 72.0)
NO_HIGH_WITHOUT_ENSEMBLE = "C2_no_high_without_ensemble"


def lead_table_high_limit(step_h: float = 0.1) -> float | None:
    """Largest lead (``step_h`` grid) at which a complete single-source hour without an
    ensemble reaches the High index class: forecast-v1's High / Moderate boundary."""
    last = None
    for i in range(int(LEAD_SHORT_H / step_h) + 1):
        lead = round(i * step_h, 6)
        if forecast_inputs(HourInputs(lead, 0.0, 0.0, 0.0)).index >= INDEX_HIGH:
            last = lead
    return last


def relabel_boundary(classes: np.ndarray, lead: np.ndarray, b1: float | None) -> np.ndarray:
    """forecast-v1 single-source classes with another High / Moderate boundary ``b1``.

    Hours that are Low keep Low; among the others, leads <= b1 become High and leads in
    (b1, 72] Moderate; leads beyond 72 h keep their class (forecast-v1 already caps them
    at Moderate without a second family). ``b1`` None: no High at all (candidate C2).
    """
    out = classes.copy()
    if b1 is None:
        out[out == HIGH] = MODERATE
        return out
    short = (out != LOW) & (lead <= LEAD_SHORT_H)
    out[short & (lead <= b1)] = HIGH
    out[short & (lead > b1)] = MODERATE
    return out


def two_class_test(
    frame: pd.DataFrame, classes: np.ndarray, *, n_boot: int, seed: int
) -> dict[str, Any]:
    """The pre-registered test restricted to the two classes that exist (Moderate < Low)."""
    scores = class_scores(frame, classes, n_boot=n_boot, seed=seed)
    gap = rmse_gap_ci(frame, classes, MODERATE, LOW, n_boot=n_boot, seed=seed)
    return {
        "counts": {c: int((classes == c).sum()) for c in CLASSES},
        "scores": scores,
        "rmse_gaps": {"low_minus_moderate": gap},
        "monotonicity": moderate_low_monotonicity(scores, gap),
    }


def boundary_scan(
    train: pd.DataFrame,
    test: pd.DataFrame | None = None,
    *,
    n_boot: int = vm.DEFAULT_N_BOOT,
    seed: int = vm.DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """TRAIN-only search for the High / Moderate boundary of single-source hours.

    The forecast-v1 classes of the TRAIN pairs (no ensemble, no verification rows) are
    relabelled for every b1 in :data:`SCAN_BOUNDARIES_H` (:func:`relabel_boundary`) and
    for C2 (no High without an ensemble or a second family); each candidate gets the
    pre-registered test on TRAIN (C2 the two-class form, since it has no High). The
    choice: the passing b1 closest to forecast-v1's boundary (:func:`lead_table_high_limit`),
    else C2 if it passes, else no change. (The candidates and this rule are not in the
    pre-registered plan; they were written after forecast-v1's TEST scores were known.)
    Only the chosen candidate is scored on ``test``, with the pre-registered three-class
    test (``test.monotonicity``) and, for a candidate without High, the two-class form as a
    diagnostic (``test.monotonicity_present_classes``). ``adoptable`` applies the plan's
    adoption rule: the chosen candidate passes the pre-registered test on TEST.
    """
    lead_train = train["lead_hours"].to_numpy(dtype=float)
    v1_train, _ = classify_frame(train, None, method=METHOD)
    current = lead_table_high_limit()
    out: dict[str, Any] = {
        "train_rows": len(train),
        "current_boundary_h": current,
        "candidates": {},
    }
    for b1 in SCAN_BOUNDARIES_H:
        labels = relabel_boundary(v1_train, lead_train, b1)
        ev = evaluate_classes(train, labels, n_boot=n_boot, seed=seed, buckets=())
        out["candidates"][f"b1={b1:g}"] = {
            "b1_h": b1,
            "counts": ev["counts"],
            "scores": ev["scores"],
            "rmse_gaps": ev["rmse_gaps"],
            "monotonicity": ev["monotonicity"],
        }
    c2 = relabel_boundary(v1_train, lead_train, None)
    out["candidates"][NO_HIGH_WITHOUT_ENSEMBLE] = {
        "b1_h": None,
        **two_class_test(train, c2, n_boot=n_boot, seed=seed),
    }
    passing = [k for k, v in out["candidates"].items() if v["monotonicity"]["passed"]]
    out["passing_on_train"] = passing
    b1s = [out["candidates"][k]["b1_h"] for k in passing if out["candidates"][k]["b1_h"]]
    if b1s and current is not None:
        choice: str | None = f"b1={min(b1s, key=lambda b: abs(b - current)):g}"
    elif NO_HIGH_WITHOUT_ENSEMBLE in passing:
        choice = NO_HIGH_WITHOUT_ENSEMBLE
    else:
        choice = None
    out["choice"] = choice
    if test is not None and choice is not None:
        lead_test = test["lead_hours"].to_numpy(dtype=float)
        v1_test, _ = classify_frame(test, None, method=METHOD)
        b1 = out["candidates"][choice]["b1_h"]
        labels = relabel_boundary(v1_test, lead_test, b1)
        ev = evaluate_classes(test, labels, n_boot=n_boot, seed=seed, buckets=())
        keys = ("counts", "scores", "rmse_gaps", "monotonicity", "monotonicity_present_classes")
        out["test"] = {k: ev[k] for k in keys if k in ev}
    out["adoptable"] = bool(out.get("test", {}).get("monotonicity", {}).get("passed", False))
    return out
