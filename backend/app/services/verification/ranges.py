"""Coverage of the app's offshore 80 % range at buoys, and a lead-dependent recalibration.

The app's breaking-height range (``surf_quality.uncertainty.breaking_uncertainty``) is
built from an OFFSHORE log-normal spread sigma_off of Hs. For one deterministic source
without an ensemble row, sigma_off = sqrt(lead_sigma(lead)^2 + SIGMA_FLOOR^2) (kind
``heuristic``); with a GEFS-Wave row, the lead table is replaced by SPREAD_SCALE x spread /
mean (kind ``ensemble_heuristic``). The offshore 80 % range that this implies is::

    p10 = Hs_model * exp(-Z80 * sigma_off),   p90 = Hs_model * exp(+Z80 * sigma_off)

:func:`app_sigma_offshore` takes sigma_off from ``breaking_uncertainty`` itself, so the
range scored here is exactly the app's offshore component (the breaking range adds the
period and translation terms, which buoys cannot verify).

Scoring (``coverage_row``): coverage = share of observed Hm0 within [p10, p90], plus the
shares below p10 and above p90 (nominally 10 % each), with cycle-block bootstrap CIs.

Recalibration (``fit_lead_scale``), fitted on a TRAINING period only. With the log error
e = ln(max(obs, 0.05)) - ln(max(model, 0.05)) and s = sigma_off, per lead bucket b::

    k_lo,b = Q0.90(-e / s) / Z80      k_hi,b = Q0.90(+e / s) / Z80

so that on the training rows 10 % of the observations fall below and 10 % above the
rescaled bounds. ``k(lead)`` interpolates linearly between the bucket centres and is
constant outside them; the rescaled range is p10 = Hs_model exp(-Z80 k_lo s), p90 =
Hs_model exp(+Z80 k_hi s). The central value is unchanged. A negative k_lo (more than
90 % of the observations above the model) puts p10 above the model value, which the
formula allows. ``fit_symmetric_scale`` is the one-factor variant (diagnostic).

The decision rule (``coverage_decision``, docs/ml-evaluation.md section 12.0): a range
counts as calibrated only when its coverage on a held-out TEST period lies within
COVERAGE_BAND at every lead bucket with at least MIN_BUCKET_N pairs. The tables also hold
the pooled row ("all", every lead >= 3 h) and the leads between and below the buckets
("other", e.g. 3-17 h and 31-41 h); neither is part of the decision.

``station_consistency`` (added after the pre-registered plan, used only to NARROW where
the label is applied): a station whose all-leads TEST coverage has a CI entirely outside
COVERAGE_BAND contradicts an 80 % range at its own location, so spots paired with it are
not labelled calibrated even when the pooled decision passes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd

from app.services.ml.dataset import assert_no_leakage
from app.services.ml.features import floored_log
from app.services.surf_quality.uncertainty import Z80, EnsembleInfo, breaking_uncertainty
from app.services.verification import metrics as vm

NOMINAL_COVERAGE = 0.80
COVERAGE_BAND = (0.75, 0.85)
MIN_BUCKET_N = 200
TAIL_QUANTILE = 0.90  # each bound should leave 10 % outside
CONFIDENCE = 0.90


# ------------------------------------------------------------------------------ app sigma
@lru_cache(maxsize=4096)
def _lead_table_sigma(lead_hours: float) -> tuple[float, str]:
    unc = breaking_uncertainty(1.0, lead_hours)
    return unc.sigma_offshore, unc.source


def app_sigma_offshore(
    lead_hours: Sequence[float] | np.ndarray,
    ens_mean: Sequence[float] | np.ndarray | None = None,
    ens_spread: Sequence[float] | np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """(sigma_off, source) per row exactly as ``breaking_uncertainty`` computes them.

    Without ensemble arrays (or where a row's mean/spread is missing) the lead table is
    used (source ``lead_table``); otherwise the ensemble spread (``ensemble_spread``, or
    ``lead_table`` when the mean is 0.05 m or less, as in the app).
    """
    lead = np.asarray(lead_hours, dtype=float)
    sigma = np.empty(lead.size, dtype=float)
    source = np.empty(lead.size, dtype=object)
    mean = None if ens_mean is None else np.asarray(ens_mean, dtype=float)
    spread = None if ens_spread is None else np.asarray(ens_spread, dtype=float)
    for i, h in enumerate(lead):
        m = None if mean is None or not math.isfinite(mean[i]) else float(mean[i])
        s = None if spread is None or not math.isfinite(spread[i]) else float(spread[i])
        if m is None or s is None:
            sigma[i], source[i] = _lead_table_sigma(float(h))
            continue
        unc = breaking_uncertainty(1.0, float(h), ensemble=EnsembleInfo(hs_mean_m=m, hs_spread_m=s))
        sigma[i], source[i] = unc.sigma_offshore, unc.source
    return sigma, source


def log_error(obs: Any, model: Any) -> np.ndarray:
    """e = ln(max(obs, 0.05)) - ln(max(model, 0.05)) (positive = observed above model)."""
    return np.asarray(floored_log(obs) - floored_log(model), dtype=float)


def bounds(
    model_hs: Any, sigma: Any, k_lo: Any = 1.0, k_hi: Any = 1.0
) -> tuple[np.ndarray, np.ndarray]:
    m = np.asarray(model_hs, dtype=float)
    s = np.asarray(sigma, dtype=float)
    lo = m * np.exp(-Z80 * np.asarray(k_lo, dtype=float) * s)
    hi = m * np.exp(Z80 * np.asarray(k_hi, dtype=float) * s)
    return lo, hi


# ------------------------------------------------------------------------------ scale
@dataclass(frozen=True)
class LeadScale:
    """Per-bucket factors on sigma_off, interpolated linearly in lead time."""

    centres_h: tuple[float, ...]
    k_lo: tuple[float, ...]
    k_hi: tuple[float, ...]
    n: tuple[int, ...]
    kind: str = "two_tail"

    def factors(self, lead_hours: Any) -> tuple[np.ndarray, np.ndarray]:
        lead = np.asarray(lead_hours, dtype=float)
        return (
            np.interp(lead, self.centres_h, self.k_lo),
            np.interp(lead, self.centres_h, self.k_hi),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "formula": "k_lo,b = Q0.90(-e/s)/Z80, k_hi,b = Q0.90(e/s)/Z80 per bucket; "
            "linear in lead between bucket centres, constant outside"
            if self.kind == "two_tail"
            else "k_b = Q0.80(|e|/s)/Z80 per bucket; linear in lead, constant outside",
            "buckets": [
                {"lead_h": c, "k_lo": round(lo, 4), "k_hi": round(hi, 4), "n": n}
                for c, lo, hi, n in zip(self.centres_h, self.k_lo, self.k_hi, self.n, strict=True)
            ],
        }


def _bucket_samples(frame: pd.DataFrame, sigma_col: str) -> list[tuple[float, np.ndarray]]:
    lead = frame["lead_hours"].to_numpy(dtype=float)
    z = log_error(frame["obs_hs_m"], frame["model_hs_m"]) / frame[sigma_col].to_numpy(float)
    out = []
    for b in vm.LEAD_BUCKETS_H:
        zb = z[vm.in_lead_bucket(lead, b) & np.isfinite(z)]
        if zb.size:
            out.append((float(b), zb))
    if not out:
        raise ValueError("no training rows in any lead bucket")
    return out


def fit_lead_scale(frame: pd.DataFrame, sigma_col: str = "sigma_off") -> LeadScale:
    """Two-tail scale on TRAINING rows (needs obs_hs_m, model_hs_m, lead_hours, sigma)."""
    samples = _bucket_samples(frame, sigma_col)
    return LeadScale(
        centres_h=tuple(c for c, _ in samples),
        k_lo=tuple(float(np.quantile(-z, TAIL_QUANTILE)) / Z80 for _, z in samples),
        k_hi=tuple(float(np.quantile(z, TAIL_QUANTILE)) / Z80 for _, z in samples),
        n=tuple(int(z.size) for _, z in samples),
    )


def fit_symmetric_scale(frame: pd.DataFrame, sigma_col: str = "sigma_off") -> LeadScale:
    """One factor per bucket, Q0.80(|e| / s) / Z80 (diagnostic variant)."""
    samples = _bucket_samples(frame, sigma_col)
    ks = tuple(float(np.quantile(np.abs(z), NOMINAL_COVERAGE)) / Z80 for _, z in samples)
    return LeadScale(
        centres_h=tuple(c for c, _ in samples),
        k_lo=ks,
        k_hi=ks,
        n=tuple(int(z.size) for _, z in samples),
        kind="symmetric",
    )


# ------------------------------------------------------------------------------ scoring
def _share_inside(lo: np.ndarray, hi: np.ndarray, o: np.ndarray) -> float:
    return float(np.mean((o >= lo) & (o <= hi))) if o.size else math.nan


def _share_below(lo: np.ndarray, hi: np.ndarray, o: np.ndarray) -> float:
    return float(np.mean(o < lo)) if o.size else math.nan


def _share_above(lo: np.ndarray, hi: np.ndarray, o: np.ndarray) -> float:
    return float(np.mean(o > hi)) if o.size else math.nan


def coverage_row(
    lo: np.ndarray,
    hi: np.ndarray,
    obs: np.ndarray,
    blocks: np.ndarray,
    *,
    n_boot: int = vm.DEFAULT_N_BOOT,
    seed: int = vm.DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Coverage, shares below / above and widths, with cycle-block bootstrap CIs."""
    keep = np.isfinite(lo) & np.isfinite(hi) & np.isfinite(obs)
    lo, hi, o, blk = lo[keep], hi[keep], obs[keep], np.asarray(blocks)[keep]
    kwargs: dict[str, Any] = {"n_boot": n_boot, "confidence": CONFIDENCE, "seed": seed}
    row: dict[str, Any] = {"n": int(o.size), "cycles": int(np.unique(blk).size)}
    for name, stat in (
        ("coverage", _share_inside),
        ("below_p10", _share_below),
        ("above_p90", _share_above),
    ):
        ci = vm.cycle_block_bootstrap(stat, blk, lo, hi, o, **kwargs)
        row[name] = vm.finite_or_none(ci.estimate)
        row[f"{name}_ci"] = [vm.finite_or_none(ci.lower), vm.finite_or_none(ci.upper)]
    row["median_width_m"] = vm.finite_or_none(float(np.median(hi - lo))) if o.size else None
    row["median_ratio_p90_p10"] = (
        vm.finite_or_none(float(np.median(hi / np.maximum(lo, 1e-6)))) if o.size else None
    )
    return row


OTHER_LEADS = "other"


def coverage_table(
    frame: pd.DataFrame,
    lo: np.ndarray,
    hi: np.ndarray,
    *,
    n_boot: int = vm.DEFAULT_N_BOOT,
    seed: int = vm.DEFAULT_BOOTSTRAP_SEED,
) -> list[dict[str, Any]]:
    """One row per lead bucket, plus "all" (every lead >= 3 h) and "other" (leads >= 3 h
    in no bucket; diagnostic)."""
    lead = frame["lead_hours"].to_numpy(dtype=float)
    obs = frame["obs_hs_m"].to_numpy(dtype=float)
    blocks = pd.to_datetime(frame["cycle_time"], utc=True).astype("int64").to_numpy()
    rows = []
    in_any = np.zeros(lead.size, dtype=bool)
    for b in (vm.ALL_LEADS_BUCKET, *vm.LEAD_BUCKETS_H):
        mask = vm.in_lead_bucket(lead, b)
        if b != vm.ALL_LEADS_BUCKET:
            in_any |= mask
        row = coverage_row(lo[mask], hi[mask], obs[mask], blocks[mask], n_boot=n_boot, seed=seed)
        rows.append({"bucket": "all" if b == vm.ALL_LEADS_BUCKET else b, **row})
    other = vm.in_lead_bucket(lead, vm.ALL_LEADS_BUCKET) & ~in_any
    row = coverage_row(lo[other], hi[other], obs[other], blocks[other], n_boot=n_boot, seed=seed)
    rows.append({"bucket": OTHER_LEADS, **row})
    return rows


def coverage_by_station(
    frame: pd.DataFrame,
    lo: np.ndarray,
    hi: np.ndarray,
    *,
    n_boot: int = vm.DEFAULT_N_BOOT,
    seed: int = vm.DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    codes = frame["station_code"].to_numpy()
    for code in sorted(set(codes.tolist())):
        mask = codes == code
        out[str(code)] = coverage_table(
            frame[mask].reset_index(drop=True), lo[mask], hi[mask], n_boot=n_boot, seed=seed
        )
    return out


def coverage_decision(
    table: list[dict[str, Any]],
    band: tuple[float, float] = COVERAGE_BAND,
    min_n: int = MIN_BUCKET_N,
) -> dict[str, Any]:
    """Pass when every lead bucket (not "all" / "other") with n >= min_n has coverage
    within band."""
    checked = [r for r in table if isinstance(r["bucket"], int) and r["n"] >= min_n]
    failing = [
        {"bucket": r["bucket"], "coverage": r["coverage"], "n": r["n"]}
        for r in checked
        if r["coverage"] is None or not band[0] <= r["coverage"] <= band[1]
    ]
    return {
        "band": list(band),
        "min_n": min_n,
        "buckets_checked": [r["bucket"] for r in checked],
        "failing": failing,
        "passed": bool(checked) and not failing,
    }


def station_consistency(
    by_station: dict[str, list[dict[str, Any]]], band: tuple[float, float] = COVERAGE_BAND
) -> dict[str, dict[str, Any]]:
    """Per station: all-leads coverage, its CI, and ``contradicts`` = "too_narrow" /
    "too_wide" when the whole CI lies below / above ``band`` (else None)."""
    out: dict[str, dict[str, Any]] = {}
    for code, table in by_station.items():
        row = next(r for r in table if r["bucket"] == "all")
        lo, hi = row["coverage_ci"]
        verdict = None
        if hi is not None and hi < band[0]:
            verdict = "too_narrow"
        elif lo is not None and lo > band[1]:
            verdict = "too_wide"
        out[code] = {
            "n": row["n"],
            "coverage": row["coverage"],
            "coverage_ci": [lo, hi],
            "contradicts": verdict,
        }
    return out


# ------------------------------------------------------------------------------ split
def train_test_split(
    frame: pd.DataFrame, test_start: pd.Timestamp, gap: timedelta
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """TEST = cycles >= test_start; TRAIN = valid times <= test_start - gap (purged)."""
    ts = pd.Timestamp(test_start)
    test = frame[frame["cycle_time"] >= ts].reset_index(drop=True)
    train = frame[frame["valid_time"] <= ts - pd.Timedelta(gap)].reset_index(drop=True)
    guard = assert_no_leakage(train, test, gap, label="range train/test")
    return train, test, guard


# ------------------------------------------------------------------------------ runner
def attach_ensemble(frame: pd.DataFrame, ensemble: pd.DataFrame) -> pd.DataFrame:
    """Inner join of deterministic pairs with ensemble rows of the same station, cycle and
    valid time (columns ``ens_mean_m``, ``ens_spread_m``)."""
    ens = ensemble[["station_id", "cycle_time", "valid_time", "hs_m", "hs_spread_m"]].rename(
        columns={"hs_m": "ens_mean_m", "hs_spread_m": "ens_spread_m"}
    )
    ens = ens[ens["ens_mean_m"].notna() & ens["ens_spread_m"].notna()]
    out = frame.merge(ens, on=["station_id", "cycle_time", "valid_time"], how="inner")
    return out.sort_values(["cycle_time", "station_id", "lead_hours"], kind="stable").reset_index(
        drop=True
    )


def with_app_sigma(frame: pd.DataFrame, *, ensemble: bool = False) -> pd.DataFrame:
    """``frame`` plus ``sigma_off`` / ``sigma_source`` as the app computes them."""
    out = frame.copy()
    sigma, source = app_sigma_offshore(
        out["lead_hours"].to_numpy(dtype=float),
        out["ens_mean_m"].to_numpy(dtype=float) if ensemble else None,
        out["ens_spread_m"].to_numpy(dtype=float) if ensemble else None,
    )
    out["sigma_off"] = sigma
    out["sigma_source"] = source
    return out


def evaluate_range(
    frame: pd.DataFrame,
    scale: LeadScale | None = None,
    *,
    n_boot: int = vm.DEFAULT_N_BOOT,
    seed: int = vm.DEFAULT_BOOTSTRAP_SEED,
    by_station: bool = True,
) -> dict[str, Any]:
    """Coverage of the app range (``scale`` None) or of the rescaled range on ``frame``."""
    model = frame["model_hs_m"].to_numpy(dtype=float)
    sigma = frame["sigma_off"].to_numpy(dtype=float)
    if scale is None:
        lo, hi = bounds(model, sigma)
    else:
        k_lo, k_hi = scale.factors(frame["lead_hours"].to_numpy(dtype=float))
        lo, hi = bounds(model, sigma, k_lo, k_hi)
    table = coverage_table(frame, lo, hi, n_boot=n_boot, seed=seed)
    out: dict[str, Any] = {"table": table, "decision": coverage_decision(table)}
    if by_station:
        out["by_station"] = coverage_by_station(frame, lo, hi, n_boot=n_boot, seed=seed)
    return out


def run_range_validation(
    frame: pd.DataFrame,
    *,
    test_start: pd.Timestamp,
    gap: timedelta,
    check: pd.DataFrame | None = None,
    n_boot: int = vm.DEFAULT_N_BOOT,
    seed: int = vm.DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """The section-12 procedure on prepared frames (``with_app_sigma`` applied).

    ``frame``: pairs of the stations being calibrated; ``check``: pairs scored with the
    TRAIN-fitted scale only (another region or period), never used for fitting.
    """
    train, test, guard = train_test_split(frame, test_start, gap)
    two_tail = fit_lead_scale(train)
    symmetric = fit_symmetric_scale(train)
    kw: dict[str, Any] = {"n_boot": n_boot, "seed": seed}
    result: dict[str, Any] = {
        "split": {
            **guard,
            "train_cycles": int(train["cycle_time"].nunique()),
            "test_cycles": int(test["cycle_time"].nunique()),
            "test_cycle_end": pd.Timestamp(test["cycle_time"].max()).isoformat(),
            "train_valid_start": pd.Timestamp(train["valid_time"].min()).isoformat(),
        },
        "sigma_sources": {
            "train": {str(k): int(v) for k, v in train["sigma_source"].value_counts().items()},
            "test": {str(k): int(v) for k, v in test["sigma_source"].value_counts().items()},
        },
        "app_range": {
            "test": evaluate_range(test, **kw),
            "train": evaluate_range(train, by_station=False, **kw),
        },
        "fitted_scale": two_tail.as_dict(),
        "fitted_range_test": evaluate_range(test, two_tail, **kw),
        "fitted_range_train": evaluate_range(train, two_tail, by_station=False, **kw),
        "symmetric_scale": symmetric.as_dict(),
        "symmetric_range_test": evaluate_range(test, symmetric, by_station=False, **kw),
    }
    result["fitted_range_test"]["station_consistency"] = station_consistency(
        result["fitted_range_test"]["by_station"]
    )
    if check is not None and not check.empty:
        result["check"] = {
            "rows": len(check),
            "stations": sorted(check["station_code"].unique().tolist()),
            "cycle_start": pd.Timestamp(check["cycle_time"].min()).isoformat(),
            "cycle_end": pd.Timestamp(check["cycle_time"].max()).isoformat(),
            "app_range": evaluate_range(check, **kw),
            "fitted_range": evaluate_range(check, two_tail, **kw),
        }
    return result


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{100.0 * value:.1f}"


def format_coverage(table: list[dict[str, Any]], title: str) -> str:
    """Plain-text coverage table (percentages)."""
    lines = [
        title,
        f"{'lead':>5} {'n':>7} {'cycles':>6} {'coverage % [90% CI]':>22} {'<p10 %':>7} "
        f"{'>p90 %':>7} {'p90/p10':>7}",
    ]
    for r in table:
        ci = r["coverage_ci"]
        cov = _pct(r["coverage"])
        if r["coverage"] is not None and ci[0] is not None and ci[1] is not None:
            cov = f"{cov} [{_pct(ci[0])},{_pct(ci[1])}]"
        ratio = "-" if r["median_ratio_p90_p10"] is None else f"{r['median_ratio_p90_p10']:.2f}"
        lines.append(
            f"{str(r['bucket']):>5} {r['n']:>7} {r['cycles']:>6} {cov:>22} "
            f"{_pct(r['below_p10']):>7} {_pct(r['above_p90']):>7} {ratio:>7}"
        )
    return "\n".join(lines)
