"""Verification statistics (pure numpy): continuous, circular, probabilistic and event scores.

Conventions
-----------
``m`` is the model (forecast) value and ``o`` the observation. Every function takes
array-likes of equal length and ignores pairs in which any value is not finite (NaN or
inf), so callers can pass columns with gaps. With no valid pair the result is ``nan``.

Continuous scores (sums over the N valid pairs; m̄ and ō are the sample means)::

    bias   = mean(m - o)
    MAE    = mean(|m - o|)
    RMSE   = sqrt(mean((m - o)^2))
    NRMSE  = sqrt(sum((m - o)^2) / sum(o^2))                    (Mentaschi et al. 2013)
    SI     = sqrt(sum(((m - m̄) - (o - ō))^2) / sum(o^2))         (Mentaschi et al. 2013)
    HH     = sqrt(sum((m - o)^2) / sum(m * o))                   (Hanna & Heinold 1985)
    CC     = Pearson correlation of m and o
    SS_ref = 1 - MSE(m, o) / MSE(ref, o)                         (skill vs a reference)

SI here is the Mentaschi form used by WW3-tools ``mvalstats``; the WMO Lead Centre
quotes the related ``std(m - o) / mean(o)`` (research notes, wave products and
validation sections). RMSE, NRMSE and SI come out systematically smaller for negatively
biased models (Mentaschi et al. 2013, Ocean Modelling 72:53-58, doi
10.1016/j.ocemod.2013.08.003), so a model that under-forecasts can beat a better one on
RMSE alone. HH and bias are therefore always reported next to RMSE and SI.

Directions are nautical degrees FROM and are never averaged as ordinary numbers: errors
are wrapped to (-180, 180] and means use unit vectors (atan2 of mean sin / mean cos).

Lead buckets (used by pairing summaries, metrics, backtests and deployment criteria):
a lead of ``h`` hours belongs to bucket ``L`` in :data:`LEAD_BUCKETS_H` when
``|h - L| <= 6`` for ``L <= 72`` and ``|h - L| <= 12`` for ``L > 72``. The buckets do
not overlap (18-30, 42-54, 66-78, 108-132, 156-180, 228-252 h); leads outside them are
counted only in the pooled :data:`ALL_LEADS_BUCKET`, which holds every lead of at least
:data:`MIN_FORECAST_LEAD_H` (3 h). Shorter leads are analysis-time rows: the observation
at the cycle time is already known when the forecast is issued (and is itself the
persistence reference), so they are paired and stored but not pooled with forecasts.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import ArrayLike, NDArray

FloatArray = NDArray[np.float64]

# ------------------------------------------------------------------------------ buckets
LEAD_BUCKETS_H: tuple[int, ...] = (24, 48, 72, 120, 168, 240)
SHORT_BUCKET_LIMIT_H = 72
SHORT_BUCKET_HALF_WIDTH_H = 6.0
LONG_BUCKET_HALF_WIDTH_H = 12.0
# Pseudo-bucket for statistics pooled over every forecast lead (>= MIN_FORECAST_LEAD_H);
# stored as lead_bucket_h = -1 in forecast_accuracy_metrics.
ALL_LEADS_BUCKET = -1
MIN_FORECAST_LEAD_H = 3.0
# Returned by :func:`lead_bucket_array` for leads that fall in no bucket.
NO_BUCKET = 0


def bucket_half_width(bucket_h: int) -> float:
    """Half width (hours) of the membership window of lead bucket ``bucket_h``."""
    return (
        SHORT_BUCKET_HALF_WIDTH_H if bucket_h <= SHORT_BUCKET_LIMIT_H else LONG_BUCKET_HALF_WIDTH_H
    )


def in_lead_bucket(lead_hours: ArrayLike, bucket_h: int) -> NDArray[np.bool_]:
    """Boolean mask: which leads belong to ``bucket_h``.

    ``ALL_LEADS_BUCKET`` selects every forecast lead (>= MIN_FORECAST_LEAD_H).
    """
    leads = np.asarray(lead_hours, dtype=float)
    if bucket_h == ALL_LEADS_BUCKET:
        return np.isfinite(leads) & (leads >= MIN_FORECAST_LEAD_H)
    return np.abs(leads - bucket_h) <= bucket_half_width(bucket_h)


def lead_bucket_array(lead_hours: ArrayLike) -> NDArray[np.int64]:
    """Bucket of every lead (:data:`NO_BUCKET` where none applies)."""
    leads = np.asarray(lead_hours, dtype=float)
    out = np.full(leads.shape, NO_BUCKET, dtype=np.int64)
    for bucket in LEAD_BUCKETS_H:
        out[in_lead_bucket(leads, bucket)] = bucket
    return out


# ------------------------------------------------------------------------------ helpers
def finite_pairs(*arrays: ArrayLike) -> tuple[FloatArray, ...]:
    """Return the arrays restricted to rows where every value is finite.

    When every row is finite the inputs themselves are returned (as flat float views),
    so callers must not write into the result.
    """
    arrs = [np.asarray(a, dtype=float).ravel() for a in arrays]
    if len({a.shape[0] for a in arrs}) > 1:
        raise ValueError("arrays must have the same length")
    if not arrs:
        return ()
    mask = np.ones(arrs[0].shape[0], dtype=bool)
    for a in arrs:
        mask &= np.isfinite(a)
    if mask.all():
        return tuple(arrs)
    return tuple(a[mask] for a in arrs)


def finite_or_none(value: float | None) -> float | None:
    """``float(value)`` when finite, else None (JSONB cannot store NaN or inf)."""
    if value is None:
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def n_pairs(m: ArrayLike, o: ArrayLike) -> int:
    mm, _ = finite_pairs(m, o)
    return int(mm.size)


def bias(m: ArrayLike, o: ArrayLike) -> float:
    """mean(m - o); positive = model too high."""
    mm, oo = finite_pairs(m, o)
    return float(np.mean(mm - oo)) if mm.size else math.nan


def mae(m: ArrayLike, o: ArrayLike) -> float:
    mm, oo = finite_pairs(m, o)
    return float(np.mean(np.abs(mm - oo))) if mm.size else math.nan


def mse(m: ArrayLike, o: ArrayLike) -> float:
    mm, oo = finite_pairs(m, o)
    return float(np.mean((mm - oo) ** 2)) if mm.size else math.nan


def rmse(m: ArrayLike, o: ArrayLike) -> float:
    """sqrt(mean((m - o)^2)). Favours negatively biased models: report with HH and bias."""
    value = mse(m, o)
    return math.sqrt(value) if math.isfinite(value) else math.nan


def nrmse(m: ArrayLike, o: ArrayLike) -> float:
    """Normalised RMSE, sqrt(sum((m - o)^2) / sum(o^2)) (Mentaschi et al. 2013)."""
    mm, oo = finite_pairs(m, o)
    denom = float(np.sum(oo**2))
    if not mm.size or denom <= 0:
        return math.nan
    return math.sqrt(float(np.sum((mm - oo) ** 2)) / denom)


def scatter_index(m: ArrayLike, o: ArrayLike) -> float:
    """Mentaschi scatter index, sqrt(sum(((m - m̄) - (o - ō))^2) / sum(o^2)).

    The bias is removed before the scatter is measured, but the normalisation by
    ``sum(o^2)`` still rewards under-forecasting models; see the module docstring.
    """
    mm, oo = finite_pairs(m, o)
    denom = float(np.sum(oo**2))
    if not mm.size or denom <= 0:
        return math.nan
    anomaly = (mm - mm.mean()) - (oo - oo.mean())
    return math.sqrt(float(np.sum(anomaly**2)) / denom)


def hanna_heinold(m: ArrayLike, o: ArrayLike) -> float:
    """HH = sqrt(sum((m - o)^2) / sum(m * o)) (Hanna & Heinold 1985).

    Recommended by Mentaschi et al. (2013) because, unlike RMSE and SI, it does not
    favour negatively biased models. Undefined (nan) when ``sum(m * o) <= 0``.
    """
    mm, oo = finite_pairs(m, o)
    denom = float(np.sum(mm * oo))
    if not mm.size or denom <= 0:
        return math.nan
    return math.sqrt(float(np.sum((mm - oo) ** 2)) / denom)


def pearson_cc(m: ArrayLike, o: ArrayLike) -> float:
    """Pearson correlation; nan with fewer than 2 pairs or zero variance."""
    mm, oo = finite_pairs(m, o)
    if mm.size < 2:
        return math.nan
    dm, do = mm - mm.mean(), oo - oo.mean()
    denom = math.sqrt(float(np.sum(dm**2)) * float(np.sum(do**2)))
    return float(np.sum(dm * do)) / denom if denom > 0 else math.nan


# Skill vs a reference is reported only when at least this many rows have the reference
# (e.g. a monthly climatology fitted on a short training period covers few test months);
# the reference sample size is always reported next to the score.
MIN_REFERENCE_PAIRS = 30


def skill_score(m: ArrayLike, o: ArrayLike, ref: ArrayLike) -> float:
    """MSE skill score vs a reference forecast: 1 - MSE(m, o) / MSE(ref, o).

    Computed on rows where model, observation and reference are all finite, so both
    errors are measured on the same sample. 1 = perfect, 0 = no better than the
    reference, negative = worse. nan when the reference is perfect (MSE_ref = 0).
    """
    mm, oo, rr = finite_pairs(m, o, ref)
    if not mm.size:
        return math.nan
    mse_ref = float(np.mean((rr - oo) ** 2))
    if mse_ref <= 0:
        return math.nan
    return 1.0 - float(np.mean((mm - oo) ** 2)) / mse_ref


def continuous_scores(m: ArrayLike, o: ArrayLike) -> dict[str, float]:
    """n, bias, MAE, RMSE, NRMSE, SI, HH and CC in one dictionary."""
    mm, oo = finite_pairs(m, o)
    return {
        "n": float(mm.size),
        "bias": bias(mm, oo),
        "mae": mae(mm, oo),
        "rmse": rmse(mm, oo),
        "nrmse": nrmse(mm, oo),
        "si": scatter_index(mm, oo),
        "hh": hanna_heinold(mm, oo),
        "cc": pearson_cc(mm, oo),
    }


# ------------------------------------------------------------------------------ circular
def wrap_degrees(x: ArrayLike) -> FloatArray:
    """Wrap angles (degrees) to the half-open interval (-180, 180]."""
    arr = np.asarray(x, dtype=float)
    wrapped = np.mod(arr + 180.0, 360.0) - 180.0  # [-180, 180)
    return np.where(wrapped == -180.0, 180.0, wrapped)


def circular_difference(m: ArrayLike, o: ArrayLike) -> FloatArray:
    """Signed smallest difference m - o in degrees, in (-180, 180] (positive = clockwise)."""
    return wrap_degrees(np.asarray(m, dtype=float) - np.asarray(o, dtype=float))


def circular_bias(m: ArrayLike, o: ArrayLike) -> float:
    """Circular mean of the errors: atan2(mean sin(m - o), mean cos(m - o)), in (-180, 180]."""
    mm, oo = finite_pairs(m, o)
    if not mm.size:
        return math.nan
    d = np.deg2rad(mm - oo)
    s, c = float(np.mean(np.sin(d))), float(np.mean(np.cos(d)))
    if math.hypot(s, c) < 1e-12:
        return math.nan
    return float(wrap_degrees(math.degrees(math.atan2(s, c))))


def circular_mae(m: ArrayLike, o: ArrayLike) -> float:
    mm, oo = finite_pairs(m, o)
    return float(np.mean(np.abs(circular_difference(mm, oo)))) if mm.size else math.nan


def circular_rmse(m: ArrayLike, o: ArrayLike) -> float:
    """sqrt(mean(wrap(m - o)^2)) with the wrapped differences."""
    mm, oo = finite_pairs(m, o)
    if not mm.size:
        return math.nan
    return math.sqrt(float(np.mean(circular_difference(mm, oo) ** 2)))


def direction_scores(m: ArrayLike, o: ArrayLike) -> dict[str, float]:
    """n, circular bias, MAE and RMSE of the wrapped differences (degrees)."""
    mm, oo = finite_pairs(m, o)
    return {
        "n": float(mm.size),
        "bias": circular_bias(mm, oo),
        "mae": circular_mae(mm, oo),
        "rmse": circular_rmse(mm, oo),
    }


# ------------------------------------------------------------------------------ CRPS
_SQRT2 = math.sqrt(2.0)
_SQRT_PI = math.sqrt(math.pi)
_erfc = np.frompyfunc(math.erfc, 1, 1)


def norm_cdf(x: ArrayLike) -> FloatArray:
    """Standard normal CDF Φ(x) = erfc(-x / √2) / 2 (math.erfc, vectorised)."""
    arr = np.asarray(x, dtype=float)
    return np.asarray(0.5 * _erfc(-arr / _SQRT2), dtype=float)


def norm_pdf(x: ArrayLike) -> FloatArray:
    arr = np.asarray(x, dtype=float)
    return np.exp(-0.5 * arr**2) / math.sqrt(2.0 * math.pi)


def crps_normal(mu: ArrayLike, sigma: ArrayLike, y: ArrayLike) -> FloatArray:
    """CRPS of N(mu, sigma^2) for observation y, closed form (Gneiting et al. 2005)::

        CRPS = sigma * [ z (2 Φ(z) - 1) + 2 φ(z) - 1/√π ],   z = (y - mu) / sigma

    In the units of y; lower is better. sigma = 0 reduces to |y - mu| (a point forecast).
    """
    mu_a, sig_a, y_a = np.broadcast_arrays(
        np.asarray(mu, dtype=float), np.asarray(sigma, dtype=float), np.asarray(y, dtype=float)
    )
    if np.any(sig_a < 0):
        raise ValueError("sigma must be >= 0")
    shape = mu_a.shape
    mu_f, sig_f, y_f = (np.atleast_1d(a).astype(float).ravel() for a in (mu_a, sig_a, y_a))
    out = np.abs(y_f - mu_f)
    pos = sig_f > 0
    if np.any(pos):
        z = (y_f[pos] - mu_f[pos]) / sig_f[pos]
        out[pos] = sig_f[pos] * (z * (2.0 * norm_cdf(z) - 1.0) + 2.0 * norm_pdf(z) - 1.0 / _SQRT_PI)
    return out.reshape(shape)


def crps_lognormal(mu_log: ArrayLike, sigma_log: ArrayLike, y: ArrayLike) -> FloatArray:
    """CRPS of a log-normal forecast (ln X ~ N(mu_log, sigma_log^2)) for observation y::

        CRPS = y (2 Φ(z) - 1) - 2 exp(mu + sigma^2/2) [ Φ(z - sigma) + Φ(sigma/√2) - 1 ]
        z = (ln y - mu) / sigma

    (Baran & Lerch 2015, doi 10.1002/qj.2521; the form implemented by scoringrules).
    For y <= 0 the limit z -> -inf applies (Φ(z) = Φ(z - sigma) = 0). Requires sigma > 0.
    """
    mu_a, sig_a, y_a = np.broadcast_arrays(
        np.asarray(mu_log, dtype=float),
        np.asarray(sigma_log, dtype=float),
        np.asarray(y, dtype=float),
    )
    if np.any(sig_a <= 0):
        raise ValueError("sigma_log must be > 0")
    positive = y_a > 0
    z = np.where(positive, (np.log(np.where(positive, y_a, 1.0)) - mu_a) / sig_a, -np.inf)
    phi_z = np.where(positive, norm_cdf(np.where(positive, z, 0.0)), 0.0)
    phi_zs = np.where(positive, norm_cdf(np.where(positive, z - sig_a, 0.0)), 0.0)
    mean = np.exp(mu_a + 0.5 * sig_a**2)
    return np.asarray(
        y_a * (2.0 * phi_z - 1.0) - 2.0 * mean * (phi_zs + norm_cdf(sig_a / _SQRT2) - 1.0),
        dtype=float,
    )


# ------------------------------------------------------------------------------ intervals
def interval_coverage(lower: ArrayLike, upper: ArrayLike, o: ArrayLike) -> float:
    """Fraction of observations inside [lower, upper] (compare with the nominal level)."""
    lo, hi, oo = finite_pairs(lower, upper, o)
    if not oo.size:
        return math.nan
    return float(np.mean((oo >= lo) & (oo <= hi)))


def mean_interval_width(lower: ArrayLike, upper: ArrayLike) -> float:
    lo, hi = finite_pairs(lower, upper)
    return float(np.mean(hi - lo)) if lo.size else math.nan


# ------------------------------------------------------------------------------ events
@dataclass(frozen=True)
class ContingencyTable:
    """2x2 table for the event ``value >= threshold``."""

    threshold: float
    hits: int
    misses: int
    false_alarms: int
    correct_negatives: int

    @property
    def n(self) -> int:
        return self.hits + self.misses + self.false_alarms + self.correct_negatives

    @property
    def pod(self) -> float:
        """Probability of detection, hits / (hits + misses)."""
        d = self.hits + self.misses
        return self.hits / d if d else math.nan

    @property
    def far(self) -> float:
        """False alarm ratio, false_alarms / (hits + false_alarms)."""
        d = self.hits + self.false_alarms
        return self.false_alarms / d if d else math.nan

    @property
    def csi(self) -> float:
        """Critical success index (threat score), hits / (hits + misses + false_alarms)."""
        d = self.hits + self.misses + self.false_alarms
        return self.hits / d if d else math.nan

    @property
    def frequency_bias(self) -> float:
        """(hits + false_alarms) / (hits + misses); 1 = event forecast as often as observed."""
        d = self.hits + self.misses
        return (self.hits + self.false_alarms) / d if d else math.nan

    @property
    def base_rate(self) -> float:
        """Observed event frequency (a CSI is meaningless without it)."""
        return (self.hits + self.misses) / self.n if self.n else math.nan

    def as_dict(self) -> dict[str, float]:
        return {
            "threshold": self.threshold,
            "n": float(self.n),
            "hits": float(self.hits),
            "misses": float(self.misses),
            "false_alarms": float(self.false_alarms),
            "correct_negatives": float(self.correct_negatives),
            "pod": self.pod,
            "far": self.far,
            "csi": self.csi,
            "frequency_bias": self.frequency_bias,
            "base_rate": self.base_rate,
        }


def contingency_table(m: ArrayLike, o: ArrayLike, threshold: float) -> ContingencyTable:
    """Count hits / misses / false alarms / correct negatives for ``value >= threshold``."""
    mm, oo = finite_pairs(m, o)
    fe, oe = mm >= threshold, oo >= threshold
    return ContingencyTable(
        threshold=float(threshold),
        hits=int(np.sum(fe & oe)),
        misses=int(np.sum(~fe & oe)),
        false_alarms=int(np.sum(fe & ~oe)),
        correct_negatives=int(np.sum(~fe & ~oe)),
    )


def event_scores(m: ArrayLike, o: ArrayLike, threshold: float) -> dict[str, float]:
    """POD, FAR, CSI, frequency bias and base rate for the event ``value >= threshold``."""
    return contingency_table(m, o, threshold).as_dict()


# ------------------------------------------------------------------------------ bootstrap
DEFAULT_BOOTSTRAP_SEED = 20261010
DEFAULT_N_BOOT = 1000


@dataclass(frozen=True)
class BootstrapCI:
    """Percentile confidence interval from a cycle-block bootstrap."""

    estimate: float
    lower: float
    upper: float
    confidence: float
    n_boot: int
    n_blocks: int

    @property
    def available(self) -> bool:
        return math.isfinite(self.lower) and math.isfinite(self.upper)

    def as_dict(self) -> dict[str, Any]:
        return {
            "estimate": finite_or_none(self.estimate),
            "lower": finite_or_none(self.lower),
            "upper": finite_or_none(self.upper),
            "confidence": self.confidence,
            "n_boot": self.n_boot,
            "n_blocks": self.n_blocks,
        }


def cycle_block_bootstrap(
    statistic: Callable[..., float],
    blocks: ArrayLike,
    *arrays: ArrayLike,
    n_boot: int = DEFAULT_N_BOOT,
    confidence: float = 0.90,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> BootstrapCI:
    """Block bootstrap that resamples whole forecast cycles, not rows.

    ``blocks`` labels each row with its block (normally the forecast cycle time). Errors
    within one cycle are strongly correlated across leads and stations, so resampling
    rows would understate the uncertainty; instead every replicate draws as many blocks
    as there are, with replacement, and keeps all rows of each drawn block. The
    statistic is evaluated as ``statistic(*arrays)`` on the original rows (estimate) and
    on each replicate; the interval is the (1-c)/2 and (1+c)/2 percentiles of the
    finite replicate values. A seeded ``numpy.random.Generator`` makes it reproducible.
    With fewer than two blocks the interval is unavailable (nan bounds).
    """
    if not 0 < confidence < 1:
        raise ValueError("confidence must be in (0, 1)")
    block_arr = np.asarray(blocks)
    arrs = [np.asarray(a) for a in arrays]
    if any(a.shape[0] != block_arr.shape[0] for a in arrs):
        raise ValueError("blocks and arrays must have the same length")
    estimate = float(statistic(*arrs)) if block_arr.size else math.nan
    if block_arr.size == 0:
        return BootstrapCI(estimate, math.nan, math.nan, confidence, 0, 0)
    _, inverse = np.unique(block_arr, return_inverse=True)
    inverse = inverse.ravel()
    order = np.argsort(inverse, kind="stable")
    counts = np.bincount(inverse)
    n_blocks = int(counts.size)
    if n_blocks < 2 or n_boot < 1:
        return BootstrapCI(estimate, math.nan, math.nan, confidence, 0, n_blocks)
    starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
    by_block = [a[order] for a in arrs]
    rng = np.random.default_rng(seed)
    replicates = np.empty(n_boot, dtype=float)
    for i in range(n_boot):
        drawn = rng.integers(0, n_blocks, size=n_blocks)
        lengths = counts[drawn]
        # Position p of the replicate maps to sorted row starts[b] + (p - first_pos[b]).
        offsets = np.repeat(starts[drawn] - (np.cumsum(lengths) - lengths), lengths)
        idx = np.arange(int(lengths.sum())) + offsets
        replicates[i] = statistic(*(a[idx] for a in by_block))
    finite = replicates[np.isfinite(replicates)]
    if finite.size < 2:
        return BootstrapCI(estimate, math.nan, math.nan, confidence, n_boot, n_blocks)
    alpha = 1.0 - confidence
    lower, upper = np.quantile(finite, [alpha / 2.0, 1.0 - alpha / 2.0])
    return BootstrapCI(estimate, float(lower), float(upper), confidence, n_boot, n_blocks)


def rmse_difference_ci(
    m_candidate: ArrayLike,
    m_reference: ArrayLike,
    o: ArrayLike,
    blocks: ArrayLike,
    **kwargs: Any,
) -> BootstrapCI:
    """CI of RMSE(candidate) - RMSE(reference) on the same rows (negative = improvement)."""
    return cycle_block_bootstrap(
        lambda a, b, y: rmse(a, y) - rmse(b, y), blocks, m_candidate, m_reference, o, **kwargs
    )


def rmse_ci(m: ArrayLike, o: ArrayLike, blocks: ArrayLike, **kwargs: Any) -> BootstrapCI:
    return cycle_block_bootstrap(rmse, blocks, m, o, **kwargs)


def bias_ci(m: ArrayLike, o: ArrayLike, blocks: ArrayLike, **kwargs: Any) -> BootstrapCI:
    return cycle_block_bootstrap(bias, blocks, m, o, **kwargs)


def skill_score_ci(
    m: ArrayLike, o: ArrayLike, ref: ArrayLike, blocks: ArrayLike, **kwargs: Any
) -> BootstrapCI:
    return cycle_block_bootstrap(skill_score, blocks, m, o, ref, **kwargs)
