"""ML post-processing: features, leakage guards, splits, deployment criteria, application.

Every number and series here is SYNTHETIC test data (hand-written values or the seeded
generator in tests/synthetic_waves.py); none of it is an observation or a forecast.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.services.ml import features as ft
from app.services.ml.apply import Correction
from app.services.ml.baselines import MIN_CLIMATOLOGY_OBS, Climatology, add_reference_predictions
from app.services.ml.dataset import (
    InsufficientDataError,
    LeakageError,
    assemble_frame,
    assert_causal_features,
    assert_no_leakage,
    chronological_split,
    leave_one_station_out,
    required_gap,
    rolling_origin_folds,
)
from app.services.ml.models import (
    FACTOR_MAX,
    FACTOR_MIN,
    TrainedCorrection,
    fit_correction,
    least_squares_scale,
    required_features,
)
from app.services.ml.registry import (
    BUCKET_MIN_N,
    MIN_TEST_PAIRS,
    OTHER_LEADS_GROUP,
    evaluate_deployment,
)
from tests.synthetic_waves import frames_for_assembly, make_case

T0 = datetime(2025, 3, 1, tzinfo=UTC)


# ------------------------------------------------------------------------------ features
def test_direction_and_time_features_use_the_unit_circle() -> None:
    s, c = ft.direction_components([0.0, 90.0, 180.0, 270.0, np.nan])
    np.testing.assert_allclose(s[:4], [0, 1, 0, -1], atol=1e-12)
    np.testing.assert_allclose(c[:4], [1, 0, -1, 0], atol=1e-12)
    assert math.isnan(s[4]) and math.isnan(c[4])
    times = pd.Series(pd.to_datetime(["2025-01-15T00:00Z", "2025-07-01T06:00Z"], utc=True))
    tf = ft.time_features(times)
    assert tf["month_sin"].iloc[0] == pytest.approx(0.0)  # January -> angle 0
    assert tf["month_cos"].iloc[1] == pytest.approx(-1.0)  # July -> angle pi
    assert tf["hour_sin"].iloc[1] == pytest.approx(1.0)  # 06 UTC -> angle pi/2


def test_log_residual_target_clips_tiny_heights() -> None:
    assert ft.log_residual(2.0, 1.0) == pytest.approx(math.log(2.0))
    # 0.0 and 0.01 m are both clipped to HS_FLOOR_M, so flat calm does not explode the loss.
    assert ft.log_residual(0.0, 0.01) == pytest.approx(0.0)
    assert ft.log_residual(1.0, 0.0) == pytest.approx(math.log(1.0 / ft.HS_FLOOR_M))


def test_features_from_wave_forecast_mapping() -> None:
    cycle = T0
    row = {
        "valid_time": cycle + timedelta(hours=48),
        "sig_wave_height_m": 2.5,
        "wave_period_s": 12.0,
        "wave_period_kind": "tp",
        "wave_direction_deg": 270.0,
        "primary_swell_height_m": 2.0,
        "primary_swell_period_s": 13.0,
        "primary_swell_direction_deg": 265.0,
        "wind_speed_kmh": 18.0,
        "wind_direction_deg": 90.0,
    }
    f = ft.features_from_wave_forecast(row, cycle_time=cycle, persistence_hs_m=2.2)
    assert set(f) == set(ft.FEATURE_NAMES)
    assert f["lead_hours"] == pytest.approx(48.0)
    assert f["model_hs_m"] == pytest.approx(2.5)
    assert f["log_model_hs"] == pytest.approx(math.log(2.5))
    assert f["model_tp_s"] == pytest.approx(12.0)
    assert f["model_dir_sin"] == pytest.approx(-1.0) and f["model_dir_cos"] == pytest.approx(
        0.0, abs=1e-12
    )
    assert f["wind_dir_sin"] == pytest.approx(1.0)
    assert f["persistence_hs_m"] == pytest.approx(2.2)
    assert f["analysis_log_error"] is None
    # A mean period (e.g. ECMWF mwp, tm01) is never fed in as a peak period.
    f2 = ft.features_from_wave_forecast({**row, "wave_period_kind": "tm01"}, cycle_time=cycle)
    assert f2["model_tp_s"] is None


# ------------------------------------------------------------------------------ guards
def _frame(cycles: list[datetime], leads: list[float], station: str = "s1") -> pd.DataFrame:
    rows = [
        {
            "station_id": 1,
            "station_code": station,
            "source_code": "synthetic",
            "family": "synthetic",
            "cycle_time": c,
            "valid_time": c + timedelta(hours=lead),
            "lead_hours": lead,
        }
        for c in cycles
        for lead in leads
    ]
    df = pd.DataFrame(rows)
    df["cycle_time"] = pd.to_datetime(df["cycle_time"], utc=True)
    df["valid_time"] = pd.to_datetime(df["valid_time"], utc=True)
    return df


def test_required_gap_is_max_lead_plus_three_days() -> None:
    assert required_gap(240) == timedelta(days=13)
    assert required_gap(24) == timedelta(days=4)


def test_leakage_guard_raises_on_deliberately_leaking_data() -> None:
    gap = required_gap(48)  # 5 days
    test = _frame([T0 + timedelta(days=30)], [24.0])
    # Last training valid time exactly at first test cycle - gap: allowed.
    ok_train = _frame([T0 + timedelta(days=30) - gap - timedelta(hours=48)], [48.0])
    info = assert_no_leakage(ok_train, test, gap)
    assert info["gap_hours"] == 120.0
    # One training row whose observation (valid time) falls one hour inside the gap.
    leaking = _frame([T0 + timedelta(days=30) - gap - timedelta(hours=47)], [48.0])
    with pytest.raises(LeakageError, match="valid_time after"):
        assert_no_leakage(pd.concat([ok_train, leaking]), test, gap)
    # A training row from the test period itself (classic random-split leak).
    with pytest.raises(LeakageError):
        assert_no_leakage(pd.concat([ok_train, test]), test, gap)
    with pytest.raises(InsufficientDataError):
        assert_no_leakage(ok_train.iloc[:0], test, gap)


def test_causal_feature_guard() -> None:
    df = _frame([T0], [24.0])
    df["persistence_observed_at"] = df["cycle_time"] - pd.Timedelta(minutes=30)
    df["obs_observed_at"] = df["valid_time"]
    assert assert_causal_features(df)["checked_rows"]["persistence_observed_at"] == 1
    late = df.assign(persistence_observed_at=df["cycle_time"] + pd.Timedelta(minutes=10))
    with pytest.raises(LeakageError, match="persistence_observed_at after cycle_time"):
        assert_causal_features(late)
    early_target = df.assign(obs_observed_at=df["cycle_time"])
    with pytest.raises(LeakageError, match="target observation"):
        assert_causal_features(early_target)


def test_chronological_split_respects_gaps() -> None:
    cycles = [T0 + timedelta(days=d) for d in range(120)]
    df = _frame(cycles, [0.0, 24.0, 72.0, 120.0])
    split = chronological_split(df)
    gap = pd.Timedelta(split.gap)
    assert split.gap == required_gap(120.0)
    assert split.train["valid_time"].max() <= split.val_start - gap
    assert split.validation["cycle_time"].min() >= split.val_start
    assert split.validation["valid_time"].max() <= split.test_start - gap
    assert split.test["cycle_time"].min() >= split.test_start
    # Train + validation (the refit set) also ends a full gap before the test period.
    assert split.development["valid_time"].max() <= split.test["cycle_time"].min() - gap
    guards = {g["guard"] for g in split.guards}
    assert "train_valid_time_before_first_test_cycle_minus_gap" in guards
    desc = split.describe()
    assert desc["gap_hours"] == pytest.approx(120 + 72)
    with pytest.raises(InsufficientDataError):
        chronological_split(_frame(cycles[:10], [0.0, 120.0]))


def test_rolling_origin_folds_expand_and_are_guarded() -> None:
    cycles = [T0 + timedelta(days=d) for d in range(100)]
    df = _frame(cycles, [24.0, 48.0])
    gap = required_gap(48.0)
    folds = rolling_origin_folds(df, 4, gap)
    assert [f.index for f in folds] == [1, 2, 3, 4]
    sizes = [len(f.train) for f in folds]
    assert sizes == sorted(sizes) and sizes[0] < sizes[-1]  # expanding window
    for fold in folds:
        assert fold.train["valid_time"].max() <= fold.test_start - pd.Timedelta(gap)
        assert fold.guard["limit"] == (fold.test_start - pd.Timedelta(gap)).isoformat()
    # Consecutive test blocks do not overlap.
    for a, b in zip(folds, folds[1:], strict=False):
        assert a.test_end < b.test_start
    assert rolling_origin_folds(df, 0) == []


def test_leave_one_station_out_keeps_chronology() -> None:
    cycles = [T0 + timedelta(days=d) for d in range(120)]
    df = pd.concat([_frame(cycles, [24.0], "a"), _frame(cycles, [24.0], "b")], ignore_index=True)
    split = chronological_split(df)
    runs = leave_one_station_out(split)
    assert [r[0] for r in runs] == ["a", "b"]
    for code, train, test in runs:
        assert set(train["station_code"]) == ({"a", "b"} - {code})
        assert set(test["station_code"]) == {code}
        assert train["valid_time"].max() <= test["cycle_time"].min() - pd.Timedelta(split.gap)


# ------------------------------------------------------------------------------ dataset
def test_assemble_frame_on_synthetic_case_is_causal() -> None:
    case = make_case("bias", n_stations=2, days=40, lead_step=12, max_lead=120, seed=3)
    fc, obs, stations = frames_for_assembly(case)
    frame = assemble_frame(fc, obs, stations)
    # Lead-0 rows are analysis-time rows and are dropped from the training rows.
    assert frame["lead_hours"].min() >= 3.0
    assert len(frame) == 2 * 40 * 10
    assert (frame["persistence_observed_at"] <= frame["cycle_time"]).all()
    assert (frame["obs_observed_at"] > frame["cycle_time"]).all()
    assert frame["analysis_log_error"].notna().all()
    np.testing.assert_allclose(
        frame[ft.TARGET_NAME], np.log(frame["obs_hs_m"]) - np.log(frame["model_hs_m"])
    )
    # The analysis error is obs(cycle) vs the lead-0 forecast of the same cycle.
    first = frame.iloc[0]
    lead0 = fc[
        (fc["station_id"] == first["station_id"])
        & (fc["cycle_time"] == first["cycle_time"])
        & (fc["lead_hours"] == 0)
    ]["hs_m"].iloc[0]
    assert first["analysis_log_error"] == pytest.approx(
        math.log(first["persistence_hs_m"]) - math.log(lead0)
    )
    assert "station_id" not in ft.FEATURE_NAMES and "station_code" not in ft.FEATURE_NAMES
    assert_causal_features(frame)


def test_climatology_uses_only_the_rows_it_is_fitted_on() -> None:
    times = pd.date_range("2025-01-01", periods=MIN_CLIMATOLOGY_OBS, freq="h", tz="UTC")
    train = pd.DataFrame(
        {"station_id": 1, "obs_observed_at": times, "obs_hs_m": 2.0, "valid_time": times}
    )
    clim = Climatology.fit(train)
    later = pd.DataFrame(
        {
            "station_id": [1, 1, 2],
            "valid_time": pd.to_datetime(["2025-01-20", "2025-02-01", "2025-01-20"], utc=True),
            "model_hs_m": [1.0, 1.0, 1.0],
            "persistence_hs_m": [np.nan, 1.5, np.nan],
        }
    )
    pred = add_reference_predictions(later, clim)
    np.testing.assert_allclose(pred["pred_climatology"], [2.0, np.nan, np.nan])
    np.testing.assert_allclose(pred["pred_persistence"], [np.nan, 1.5, np.nan])
    # Too few observations in a month: no climatology rather than a noisy one.
    assert Climatology.fit(train.iloc[:-1]).table == {}


# ------------------------------------------------------------------------------ models
class ConstantEstimator:
    """Test double: predicts the same log residual for every row."""

    def __init__(self, value: float) -> None:
        self.value = value

    def predict(self, X: np.ndarray) -> np.ndarray:
        return np.full(len(X), self.value)


def _correction(value: float, **kw: Any) -> TrainedCorrection:
    defaults: dict[str, Any] = {
        "kind": "test",
        "params": {},
        "feature_names": ft.FEATURE_NAMES,
        "estimator": ConstantEstimator(value),
        "required_features": ("lead_hours", "model_hs_m", "persistence_hs_m"),
        "lead_min": 6.0,
        "lead_max": 120.0,
    }
    defaults.update(kw)
    return TrainedCorrection(**defaults)


def _matrix(rows: Sequence[Mapping[str, float | None]]) -> np.ndarray:
    return np.vstack([ft.feature_matrix(r, ft.FEATURE_NAMES) for r in rows])


def test_factor_is_clamped_and_never_extrapolated() -> None:
    base: dict[str, float | None] = {"lead_hours": 24.0, "model_hs_m": 2.0, "persistence_hs_m": 1.8}
    big = _correction(math.log(5.0))
    factors, applied = big.factors(_matrix([base]))
    assert factors[0] == FACTOR_MAX and applied[0]
    small = _correction(math.log(0.1))
    assert small.factors(_matrix([base]))[0][0] == FACTOR_MIN
    mild = _correction(math.log(0.9))
    rows = [
        base,
        {**base, "lead_hours": 121.0},  # beyond the trained range: raw
        {**base, "lead_hours": 3.0},  # below the trained range: raw
        {**base, "persistence_hs_m": None},  # required feature missing: raw
        {**base, "wind_speed_kmh": None},  # optional feature missing: still applied
    ]
    factors, applied = mild.factors(_matrix(rows))
    np.testing.assert_allclose(factors, [0.9, 1.0, 1.0, 1.0, 0.9])
    assert applied.tolist() == [True, False, False, False, True]


@pytest.mark.parametrize("kind", ["linear", "rf", "gbt"])
def test_candidates_train_without_a_feature_the_source_lacks(kind: str) -> None:
    """GFS-Wave station bulletins carry no wind: every candidate must still train."""
    case = make_case("bias", n_stations=1, days=30, lead_step=12, max_lead=120, seed=4)
    fc, obs, stations = frames_for_assembly(case)
    fc[["wind_speed_kmh", "wind_direction_deg"]] = np.nan
    frame = assemble_frame(fc, obs, stations)
    grid: dict[str, dict[str, Any]] = {
        "linear": {"alpha": 1.0},
        "rf": {"n_estimators": 10, "max_depth": 4, "min_samples_leaf": 5},
        "gbt": {"max_iter": 20, "max_leaf_nodes": 7},
    }
    params = grid[kind]
    model = fit_correction(kind, params, frame, ft.FEATURE_NAMES)
    assert set(model.unused_features) == {"wind_speed_kmh", "wind_dir_sin", "wind_dir_cos"}
    assert "wind_speed_kmh" not in model.feature_names
    corrected = model.correct(frame)
    assert np.isfinite(corrected).all()
    # The synthetic model over-forecasts, so the correction lowers Hs on average.
    assert float(np.mean(corrected / frame["model_hs_m"])) < 1.0


def test_required_features_follow_training_coverage() -> None:
    train = pd.DataFrame(
        {
            "lead_hours": np.arange(100.0),
            "model_hs_m": 1.0,
            "persistence_hs_m": [1.0] * 99 + [np.nan],
            "wind_speed_kmh": [np.nan] * 50 + [1.0] * 50,
        }
    )
    req = required_features(
        train, ["lead_hours", "model_hs_m", "persistence_hs_m", "wind_speed_kmh"]
    )
    assert req == ("lead_hours", "model_hs_m", "persistence_hs_m")


def test_least_squares_scale_is_bounded() -> None:
    assert least_squares_scale(np.array([2.0, 4.0]), np.array([2.0, 4.0])) == pytest.approx(1.0)
    assert least_squares_scale(np.array([2.1, 4.2]), np.array([2.0, 4.0])) == pytest.approx(1.05)
    assert least_squares_scale(np.array([4.0]), np.array([1.0])) == pytest.approx(1.1)
    assert least_squares_scale(np.array([np.nan]), np.array([1.0])) == 1.0


def _applied_correction(value: float) -> Correction:
    return Correction(
        version_id=1,
        label="offshore_hs_residual@v1",
        sha256="0" * 64,
        model=_correction(value),
        station_codes=frozenset({"synthetic:station-1"}),
        spot_slugs=frozenset({"synthetic-spot-1"}),
        sources=frozenset({"synthetic_gfswave"}),
        families=frozenset({"ncep_gfs"}),
        lead_min_h=6.0,
        lead_max_h=120.0,
        training_end=None,
    )


def test_correction_applies_only_where_trained() -> None:
    corr = _applied_correction(math.log(0.8))
    assert corr.applies_to("synthetic-spot-1", 48)
    assert not corr.applies_to("synthetic-spot-2", 48)  # not paired with a training buoy
    assert not corr.applies_to("synthetic-spot-1", 200)  # beyond the trained lead range
    assert not corr.applies_to("synthetic-spot-1", 2)
    assert not corr.applies_to("synthetic-spot-1", 48, family="ecmwf_ifs")
    assert corr.applies_to("synthetic-spot-1", 48, family="ncep_gfs")


def test_correct_hs_returns_raw_unchanged_when_it_cannot_apply() -> None:
    corr = _applied_correction(math.log(0.8))
    feats = ft.features_from_values(
        cycle_time=T0,
        valid_time=T0 + timedelta(hours=48),
        hs_m=2.5,
        tp_s=12.0,
        period_kind="tp",
        direction_deg=250.0,
        persistence_hs_m=2.0,
    )
    value, info = corr.correct_hs(feats)
    assert value == pytest.approx(2.0)
    assert info["factor"] == pytest.approx(0.8) and info["applied"]
    assert info["model"] == "offshore_hs_residual@v1" and info["raw"] == pytest.approx(2.5)
    value, info = corr.correct_hs({**feats, "persistence_hs_m": None})
    assert value == pytest.approx(2.5) and info["factor"] == 1.0 and not info["applied"]
    assert "persistence_hs_m" in info["reason"]
    value, info = corr.correct_hs({**feats, "lead_hours": 300.0})
    assert value == pytest.approx(2.5) and "lead" in info["reason"]
    value, info = corr.correct_hs({**feats, "model_hs_m": None})
    assert value is None and info["reason"] == "missing model Hs"
    # Clamp: a model that asks for x5 is limited to x2.
    huge = _applied_correction(math.log(5.0))
    assert huge.correct_hs(feats)[0] == pytest.approx(5.0)


# ------------------------------------------------------------------------------ criteria
def _criteria(decision: dict[str, Any]) -> dict[str, bool]:
    return {c["id"]: c["passed"] for c in decision["criteria"]}


def _scenario(n_cycles: int = 60, per_cycle: int = 12, seed: int = 5) -> dict[str, np.ndarray]:
    """SYNTHETIC test arrays: raw over-forecasts by 30 %; leads cycle through 24-168 h."""
    rng = np.random.default_rng(seed)
    n = n_cycles * per_cycle
    obs = rng.gamma(4.0, 0.5, n) + 0.3
    raw = obs * 1.3 * np.exp(rng.normal(0, 0.1, n))
    leads = np.tile(np.array([24.0, 48.0, 72.0, 120.0, 168.0, 240.0]), n // 6 + 1)[:n]
    cycles = np.repeat(np.arange(n_cycles), per_cycle)
    return {"obs": obs, "raw": raw, "lead": leads, "cycles": cycles}


def _decide(
    s: dict[str, np.ndarray], pred: np.ndarray, kind: str = "gbt", linear: Any = None
) -> dict[str, Any]:
    return evaluate_deployment(
        kind=kind,
        obs=s["obs"],
        pred=pred,
        raw=s["raw"],
        lead_hours=s["lead"],
        cycles=s["cycles"],
        linear_pred=linear,
        n_boot=300,
    )


def test_all_criteria_pass_for_a_genuine_correction() -> None:
    s = _scenario()
    good = s["raw"] / 1.3
    decision = _decide(s, good, linear=good * 1.005)
    assert decision["passed_all"], decision["failed"]
    assert decision["n_test"] == 720
    assert set(_criteria(decision)) == {"a", "b", "c", "d", "e", "f"}


def test_criterion_a_minimum_rmse_reduction() -> None:
    s = _scenario()
    # Shrink the over-forecast only a little: RMSE drops, but by less than 5 %.
    slight = s["raw"] / 1.01
    decision = _decide(s, slight, linear=slight)
    crit = _criteria(decision)
    assert not crit["a"] and "a" in decision["failed"]
    good = _decide(s, s["raw"] / 1.3, linear=s["raw"] / 1.3)
    assert _criteria(good)["a"]


def test_criterion_b_bootstrap_ci_must_exclude_zero() -> None:
    n_cycles, per = 20, 12
    obs = np.full(n_cycles * per, 2.0)
    cycles = np.repeat(np.arange(n_cycles), per)
    raw = obs + np.where(cycles == 7, 1.0, 0.1)  # one cycle with a large error
    s = {"obs": obs, "raw": raw, "lead": np.full(obs.size, 24.0), "cycles": cycles}
    # Identical to raw except in that ONE cycle, where it is perfect: RMSE falls by 60 %
    # overall, but about a third of the cycle resamples miss cycle 7 (difference 0), so
    # the 90 % CI of RMSE(candidate) - RMSE(raw) reaches 0 and the gain is not robust.
    pred = np.where(cycles == 7, obs, raw)
    decision = _decide(s, pred, linear=pred)
    crit = _criteria(decision)
    assert crit["a"], decision["criteria"][0]["detail"]
    assert not crit["b"]
    ci = next(c for c in decision["criteria"] if c["id"] == "b")["value"]
    assert ci["upper"] == pytest.approx(0.0, abs=1e-12) and ci["lower"] < 0
    # The same 60 % gain spread over every cycle is robust.
    steady = _decide(s, obs + 0.4 * (raw - obs), linear=obs + 0.4 * (raw - obs))
    assert _criteria(steady)["b"]


def test_criterion_c_no_lead_bucket_may_worsen() -> None:
    s = _scenario()
    pred = s["raw"] / 1.3
    in_120 = s["lead"] == 120.0
    assert in_120.sum() >= BUCKET_MIN_N
    pred[in_120] = s["raw"][in_120] * 1.2  # much worse than raw in the 120 h bucket
    decision = _decide(s, pred, linear=pred)
    crit = _criteria(decision)
    assert crit["a"] and not crit["c"]
    # The same degradation in a bucket with fewer than BUCKET_MIN_N pairs is not checked.
    s_small = _scenario(n_cycles=60)
    small_pred = s_small["raw"] / 1.3
    few = np.flatnonzero(s_small["lead"] == 120.0)[: BUCKET_MIN_N - 1]
    s_small["lead"][s_small["lead"] == 120.0] = 100.0  # move the rest out of every bucket
    s_small["lead"][few] = 120.0
    small_pred[few] = s_small["raw"][few] * 1.2
    decision = _decide(s_small, small_pred, linear=small_pred)
    assert _criteria(decision)["c"]
    buckets = next(c for c in decision["criteria"] if c["id"] == "c")["value"]["buckets"]
    assert buckets["120"] == {"n": BUCKET_MIN_N - 1, "checked": False}


def test_criterion_c_also_checks_leads_outside_every_bucket() -> None:
    """SYNTHETIC: the buckets leave gaps (79-107 h, > 252 h ...) where the correction is
    still applied; worsening only those leads must fail (c) as well."""
    s = _scenario()
    pred = s["raw"] / 1.3
    gap = s["lead"] == 168.0
    s["lead"][gap] = 300.0  # beyond the last bucket (228-252 h)
    pred[gap] = s["raw"][gap] * 1.2
    decision = _decide(s, pred, linear=pred)
    crit = _criteria(decision)
    assert crit["a"] and not crit["c"]
    buckets = next(c for c in decision["criteria"] if c["id"] == "c")["value"]["buckets"]
    assert buckets[OTHER_LEADS_GROUP]["checked"] and buckets[OTHER_LEADS_GROUP]["n"] == 120
    assert buckets[OTHER_LEADS_GROUP]["rmse_change"] > 0.03


def test_criterion_d_must_beat_or_tie_the_linear_baseline() -> None:
    s = _scenario()
    linear = s["raw"] / 1.3
    worse = linear * 1.06  # still far better than raw, but clearly worse than linear
    assert not _criteria(_decide(s, worse, linear=linear))["d"]
    tie = linear * 1.002
    assert _criteria(_decide(s, tie, linear=linear))["d"]
    # The linear baseline itself passes (d) by definition.
    assert _criteria(_decide(s, linear, kind="linear", linear=linear))["d"]
    # Without a linear baseline to compare against, a non-linear candidate cannot pass.
    assert not _criteria(_decide(s, linear, kind="gbt", linear=None))["d"]


def test_criterion_e_minimum_test_pairs() -> None:
    s = _scenario(n_cycles=25, per_cycle=12)  # 300 pairs
    good = s["raw"] / 1.3
    assert _criteria(_decide(s, good, linear=good))["e"]
    cut = {k: v[: MIN_TEST_PAIRS - 1] for k, v in s.items()}
    decision = _decide(cut, good[: MIN_TEST_PAIRS - 1], linear=good[: MIN_TEST_PAIRS - 1])
    assert not _criteria(decision)["e"] and decision["n_test"] == MIN_TEST_PAIRS - 1


def test_criterion_f_rejects_rmse_gains_from_damping() -> None:
    """A forecast shrunk towards zero can lower RMSE while HH rises (Mentaschi 2013)."""
    rng = np.random.default_rng(11)
    n_cycles, per = 60, 12
    obs = np.full(n_cycles * per, 1.0)
    # Unbiased (mean 1) but very noisy log-normal forecasts.
    raw = np.exp(rng.normal(-0.5 * 0.9**2, 0.9, obs.size))
    s = {
        "obs": obs,
        "raw": raw,
        "lead": np.full(obs.size, 24.0),
        "cycles": np.repeat(np.arange(n_cycles), per),
    }
    damped = 0.35 * raw
    decision = _decide(s, damped, linear=damped)
    crit = _criteria(decision)
    assert crit["a"] and crit["b"]  # RMSE "improves" ...
    assert not crit["f"]  # ... but only by damping, which HH exposes
    assert not decision["passed_all"]
