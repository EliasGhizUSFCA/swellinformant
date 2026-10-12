"""Forecast-confidence classes of archived forecasts and their scoring against buoys.

Every value here is SYNTHETIC (hand-written or seeded random numbers).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.services.surf_quality.forecast_confidence import (
    HIGH,
    LOW,
    MODERATE,
    ForecastInputs,
    Verification,
    classify_forecast,
)
from app.services.verification import confidence_classes as cc

START = pd.Timestamp(datetime(2025, 1, 1, tzinfo=UTC))


def _hour(lead: float, **kw: object) -> cc.HourInputs:
    base = {"swell_hs": 1.0, "swell_tp": 12.0, "swell_dir": 280.0}
    base.update(kw)  # type: ignore[arg-type]
    return cc.HourInputs(lead_hours=lead, **base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("lead", "index_class", "expected"),
    [
        (12.0, HIGH, MODERATE),  # forecast-v1.1: one model, no ensemble -> at most Moderate
        (52.0, HIGH, MODERATE),  # lead-table index 70 (High under forecast-v1)
        (53.0, MODERATE, MODERATE),  # index 69
        (100.0, MODERATE, MODERATE),  # single source beyond 3 days, no ensemble: capped
        (150.0, MODERATE, MODERATE),  # index 45
        (151.0, LOW, LOW),  # index 44
        (230.0, LOW, LOW),
    ],
)
def test_single_source_classes_follow_the_lead_table(
    lead: float, index_class: str, expected: str
) -> None:
    fc = classify_forecast(cc.forecast_inputs(_hour(lead)))
    assert fc.index_class == index_class and fc.cls == expected
    if lead <= 72.0:
        assert any(r.rule == "single_source_no_ensemble" for r in fc.rules)
    inp = cc.forecast_inputs(_hour(lead))
    assert inp.families == ("ncep_gfs",) and inp.sigma_source == "lead_table"
    assert not inp.missing_wind and not inp.stale and inp.verification == ()


def test_missing_swell_and_wind_counted_present() -> None:
    assert classify_forecast(cc.forecast_inputs(_hour(24.0, swell_tp=None))).cls == LOW
    # Wind is counted present (the app's NOAA source always has it): index at 24 h is
    # 100 exp(-0.08/0.4) = 81.9 with full completeness.
    assert cc.forecast_inputs(_hour(24.0)).index == 82


def test_ensemble_and_verification_inputs() -> None:
    small = cc.forecast_inputs(_hour(24.0, ens_mean=2.0, ens_spread=0.1))
    assert small.sigma_source == "ensemble_spread" and small.ensemble is not None
    assert classify_forecast(small).cls == HIGH  # a small ensemble spread allows High
    wide = classify_forecast(cc.forecast_inputs(_hour(24.0, ens_mean=2.0, ens_spread=0.9)))
    assert wide.cls == LOW and any(r.rule == "spread_very_large" for r in wide.rules)
    poor = Verification("station", "synthetic:a", "gfswave_bulletin", 24, 300, 0.6, 0.2, True)
    capped = classify_forecast(
        cc.forecast_inputs(_hour(24.0, ens_mean=2.0, ens_spread=0.1, verification=poor))
    )
    assert capped.cls == MODERATE and any(r.rule == "verification_poor" for r in capped.rules)


def _frame(leads: list[float], stations: list[str], cycles: int) -> pd.DataFrame:
    rows = []
    for c in range(cycles):
        cycle = START + pd.Timedelta(days=c)
        for code in stations:
            for h in leads:
                rows.append(
                    {
                        "station_code": code,
                        "cycle_time": cycle,
                        "valid_time": cycle + pd.Timedelta(hours=h),
                        "lead_hours": h,
                        "model_hs_m": 1.0,
                        "obs_hs_m": 1.0,
                        "persistence_hs_m": 1.0,
                        "swell1_hs_m": 1.0,
                        "swell1_tp_s": 12.0,
                        "swell1_dir_sin": 0.0,
                    }
                )
    return pd.DataFrame(rows)


def test_causal_verification_uses_only_pairs_before_the_cycle() -> None:
    """SYNTHETIC: perfect forecasts until day 60, then 1 m too high; persistence perfect."""
    leads = [18.0 + i for i in range(13)]  # every lead of the 24 h bucket
    pairs = _frame(leads, ["synthetic:a"], 90).assign(persistence_hs_m=1.1)
    late = pairs["valid_time"] >= START + pd.Timedelta(days=60)
    pairs.loc[late, "model_hs_m"] = 2.0
    pairs.loc[~late, "model_hs_m"] = 1.0 + 0.05 * np.sin(np.arange(int((~late).sum())))
    cycles = pd.DataFrame(
        {
            "station_code": ["synthetic:a"] * 3,
            "cycle_time": [START + pd.Timedelta(days=d) for d in (10, 40, 75)],
        }
    )
    ver = cc.causal_verification(pairs, cycles, window_days=30)
    # Day 10: fewer than 200 pairs in the window before it (no row, as in the app).
    assert ("synthetic:a", cycles.cycle_time[0], 24) not in ver
    early = ver[("synthetic:a", cycles.cycle_time[1], 24)]
    # Day 40: 30 days of valid times x 13 leads, only good forecasts, whatever comes later.
    assert early.n == 30 * 13 and not early.as_dict()["poor"]
    assert early.near_spot and early.scope == "station"
    later = ver[("synthetic:a", cycles.cycle_time[2], 24)]
    assert later.as_dict()["poor"]  # the bad forecasts after day 60 are now in the window
    assert later.skill_vs_persistence is not None and later.skill_vs_persistence < 0


def test_scores_monotonicity_and_ties() -> None:
    """SYNTHETIC errors: High 0.1 m, Moderate 0.2 m, Low 0.4 m, over 40 cycles."""
    frame = _frame([24.0, 100.0, 200.0], ["synthetic:a"], 40)
    err = frame["lead_hours"].map({24.0: 0.1, 100.0: 0.2, 200.0: 0.4})
    sign = np.where(np.arange(len(frame)) % 2 == 0, 1.0, -1.0)
    frame["obs_hs_m"] = frame["model_hs_m"] + sign * err
    classes = frame["lead_hours"].map({24.0: HIGH, 100.0: MODERATE, 200.0: LOW}).to_numpy()
    ev = cc.evaluate_classes(frame, classes, n_boot=100, buckets=(24,))
    assert ev["counts"] == {HIGH: 40, MODERATE: 40, LOW: 40}
    assert ev["scores"][MODERATE]["median_abs_error"] == 0.2
    assert ev["scores"][HIGH]["rmse"] == pytest.approx(0.1)
    assert ev["monotonicity"]["passed"]
    # Equal medians (up to floating-point noise) are a tie, not an increase.
    tied = frame.copy()
    tied.loc[tied["lead_hours"] == 100.0, "obs_hs_m"] = (tied["model_hs_m"] + sign * (0.1 + 1e-12))[
        tied["lead_hours"] == 100.0
    ]
    ev = cc.evaluate_classes(tied, classes, n_boot=100, buckets=())
    assert not ev["monotonicity"]["checks"]["median_abs_error_increasing"]
    assert not ev["monotonicity"]["passed"]
    # A class without rows fails the test.
    ev = cc.evaluate_classes(frame, np.where(classes == LOW, MODERATE, classes), n_boot=50)
    assert not ev["monotonicity"]["passed"]
    assert "monotonicity_present_classes" not in ev  # only reported when High is missing
    no_high = cc.evaluate_classes(frame, np.where(classes == HIGH, MODERATE, classes), n_boot=50)
    assert not no_high["monotonicity"]["passed"]
    assert no_high["monotonicity_present_classes"]["passed"]


def test_classify_frame_and_rule_effect() -> None:
    frame = _frame([24.0, 60.0, 100.0, 160.0], ["synthetic:a"], 5)
    frame.loc[frame.index[0], "swell1_tp_s"] = np.nan
    classes, rules = cc.classify_frame(frame)
    assert list(classes[1:4]) == [MODERATE, MODERATE, LOW]  # 60, 100, 160 h
    assert classes[0] == LOW and any("missing_swell" in r for r in rules[0])
    assert set(classes[frame["lead_hours"] == 160.0]) == {LOW}
    assert any("single_source_over_3d" in r for r in rules[2])
    assert any("single_source_no_ensemble" in r for r in rules[1])
    effect = cc.rule_effect_by_bucket(frame, rules, "missing_swell", n_boot=20, seed=1)
    assert effect["24"]["fired"]["n"] == 1 and effect["24"]["not_fired"]["n"] == 4


def test_window_cycles_are_unique() -> None:
    frame = _frame([24.0, 48.0], ["synthetic:a", "synthetic:b"], 3)
    out = cc.window_cycles(frame)
    assert len(out) == 6 and set(out.columns) == {"station_code", "cycle_time"}
    assert timedelta(days=2) == out["cycle_time"].max() - out["cycle_time"].min()


def test_forecast_v1_is_v1_1_without_the_new_cap() -> None:
    """forecast-v1 (derived) gives High to complete single-source hours up to 52 h."""
    assert cc.lead_table_high_limit() == 52.0
    frame = _frame([6.0, 24.0, 52.0, 53.0, 100.0, 160.0, 250.0], ["synthetic:a"], 2)
    v11, rules11 = cc.classify_frame(frame)
    v1, rules1 = cc.classify_frame(frame, method=cc.METHOD_V1)
    assert list(v1[:7]) == [HIGH, HIGH, HIGH, MODERATE, MODERATE, LOW, LOW]
    assert list(v11[:7]) == [MODERATE, MODERATE, MODERATE, MODERATE, MODERATE, LOW, LOW]
    assert not any("single_source_no_ensemble" in r for rs in rules1 for r in rs)
    assert any("single_source_no_ensemble" in r for r in rules11[0])
    # With a small ensemble spread both methods agree (the new cap needs "no ensemble").
    ens = frame.assign(ens_mean_m=2.0, ens_spread_m=0.1)
    a, _ = cc.classify_frame(ens, ensemble=True)
    b, _ = cc.classify_frame(ens, ensemble=True, method=cc.METHOD_V1)
    assert list(a) == list(b) and a[0] == HIGH
    with pytest.raises(ValueError, match="unknown method"):
        cc.classify_frame(frame, method="forecast-v0")


def test_class_without_recomputes_caps_and_downgrades() -> None:
    poor = Verification("station", "synthetic:a", "gfswave_bulletin", 24, 300, 0.6, 0.2, True)
    fc = classify_forecast(cc.forecast_inputs(_hour(24.0, verification=poor)))
    assert fc.cls == MODERATE  # both the v1.1 cap and verification_poor
    assert cc.class_without(fc, cc.V11_RULES) == MODERATE  # verification_poor still caps
    assert cc.class_without(fc, frozenset({"single_source_no_ensemble", "verification_poor"}))
    changed = classify_forecast(
        ForecastInputs(
            index=90,
            sigma_source="lead_table",
            lead_hours=24.0,
            source_code="synthetic",
            families=("ncep_gfs", "ecmwf_ifs"),
            run_change_ratio=2.0,
        )
    )
    assert changed.cls == MODERATE and any(r.effect == "downgrade" for r in changed.rules)
    assert cc.class_without(changed, frozenset({"run_change"})) == HIGH
    assert cc.class_without(changed, cc.V11_RULES) == MODERATE  # two families: no new cap


def test_relabel_boundary_and_c2_equal_v1_1() -> None:
    frame = _frame([12.0, 30.0, 48.0, 60.0, 72.0, 100.0], ["synthetic:a"], 3)
    frame.loc[frame.index[0], "swell1_dir_sin"] = np.nan  # missing direction: Low
    v1, _ = cc.classify_frame(frame, method=cc.METHOD_V1)
    lead = frame["lead_hours"].to_numpy(dtype=float)
    at36 = cc.relabel_boundary(v1, lead, 36.0)
    assert at36[0] == LOW  # Low hours keep Low
    assert list(at36[1:6]) == [HIGH, MODERATE, MODERATE, MODERATE, MODERATE]
    assert list(cc.relabel_boundary(v1, lead, 72.0)[1:6]) == [HIGH] * 4 + [MODERATE]
    v11, _ = cc.classify_frame(frame)
    assert list(cc.relabel_boundary(v1, lead, None)) == list(v11)  # C2 is forecast-v1.1


def test_boundary_scan_on_synthetic_errors() -> None:
    """SYNTHETIC: error grows with lead only beyond 72 h, so no High/Moderate boundary
    inside 3 days separates errors, and C2 (Moderate < Low) passes."""
    leads = [6.0, 24.0, 48.0, 60.0, 72.0, 168.0]
    frame = _frame(leads, ["synthetic:a"], 60)
    rng = np.random.default_rng(5)
    scale = np.where(frame["lead_hours"] > 72.0, 0.6, 0.2)
    frame["obs_hs_m"] = frame["model_hs_m"] + scale * rng.normal(size=len(frame))
    frame.loc[frame["lead_hours"] == 168.0, "swell1_tp_s"] = np.nan  # those hours are Low
    train, test = frame[frame["cycle_time"] < START + pd.Timedelta(days=40)], frame
    out = cc.boundary_scan(train, test, n_boot=60, seed=3)
    assert out["current_boundary_h"] == 52.0
    assert set(out["candidates"]) == {f"b1={b:g}" for b in cc.SCAN_BOUNDARIES_H} | {
        cc.NO_HIGH_WITHOUT_ENSEMBLE
    }
    assert out["choice"] in (*out["passing_on_train"], None)
    c2 = out["candidates"][cc.NO_HIGH_WITHOUT_ENSEMBLE]
    assert c2["counts"][HIGH] == 0 and c2["monotonicity"]["passed"]
    assert out["choice"] == cc.NO_HIGH_WITHOUT_ENSEMBLE
    assert out["test"]["monotonicity"]["passed"]
    two = cc.two_class_test(test, np.full(len(test), MODERATE, dtype=object), n_boot=20, seed=1)
    assert not two["monotonicity"]["passed"]  # no Low rows
