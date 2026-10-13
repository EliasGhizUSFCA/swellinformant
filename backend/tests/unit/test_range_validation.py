"""Coverage of the offshore 80 % range and its lead-dependent recalibration.

Every value here is SYNTHETIC (seeded random numbers or hand-written arrays); nothing is
an observation or a model forecast.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.services.ml.dataset import LeakageError
from app.services.surf_quality.uncertainty import (
    SIGMA_FLOOR,
    Z80,
    EnsembleInfo,
    breaking_uncertainty,
    lead_sigma,
)
from app.services.verification import ranges

START = datetime(2025, 1, 1, tzinfo=UTC)


def test_app_sigma_is_the_offshore_part_of_breaking_uncertainty() -> None:
    leads = [3.0, 24.0, 60.0, 130.0, 240.0, 300.0]
    sigma, source = ranges.app_sigma_offshore(leads)
    for h, s, src in zip(leads, sigma, source, strict=True):
        assert s == pytest.approx(np.hypot(lead_sigma(h), SIGMA_FLOOR))
        assert s == breaking_uncertainty(3.0, h).sigma_offshore and src == "lead_table"
    mean = [2.0, 0.04, np.nan, 1.5, 3.0, 2.0]
    spread = [0.2, 0.01, 0.3, np.nan, 0.9, 5.0]
    sigma, source = ranges.app_sigma_offshore(leads, mean, spread)
    assert sigma[0] == pytest.approx(np.hypot(1.05 * 0.2 / 2.0, SIGMA_FLOOR))
    assert source[0] == "ensemble_spread"
    # Mean <= 0.05 m (flat sea) and missing values fall back to the lead table, as in the app.
    assert list(source[1:4]) == ["lead_table"] * 3
    # The app caps sigma_f at SIGMA_MAX before adding the floor.
    expected = breaking_uncertainty(
        1.0, 300.0, ensemble=EnsembleInfo(hs_mean_m=2.0, hs_spread_m=5.0)
    ).sigma_offshore
    assert sigma[5] == pytest.approx(expected)


def test_bounds_and_scale_interpolation() -> None:
    lo, hi = ranges.bounds([2.0], [0.2])
    assert lo[0] == pytest.approx(2.0 * np.exp(-Z80 * 0.2))
    assert hi[0] == pytest.approx(2.0 * np.exp(Z80 * 0.2))
    scale = ranges.LeadScale((24.0, 48.0), (0.5, 1.0), (2.0, 3.0), (300, 300))
    k_lo, k_hi = scale.factors([0.0, 24.0, 36.0, 48.0, 400.0])
    assert list(k_lo) == [0.5, 0.5, 0.75, 1.0, 1.0]  # constant outside the centres
    assert list(k_hi) == [2.0, 2.0, 2.5, 3.0, 3.0]
    assert scale.as_dict()["buckets"][0] == {"lead_h": 24.0, "k_lo": 0.5, "k_hi": 2.0, "n": 300}


def _synthetic_pairs(n_cycles: int, bias: float, sd: float, seed: int) -> pd.DataFrame:
    """SYNTHETIC pairs: ln(obs) = ln(model) + bias + sd * N(0, 1), hourly leads 3-240 h."""
    rng = np.random.default_rng(seed)
    leads = np.arange(3.0, 241.0, 3.0)
    rows = []
    for c in range(n_cycles):
        cycle = START + timedelta(days=2 * c)
        model = rng.uniform(0.8, 3.0, leads.size)
        obs = model * np.exp(bias + sd * rng.normal(size=leads.size))
        for h, m, o in zip(leads, model, obs, strict=True):
            rows.append(
                {
                    "station_code": "synthetic:a",
                    "station_id": 1,
                    "cycle_time": pd.Timestamp(cycle),
                    "valid_time": pd.Timestamp(cycle + timedelta(hours=float(h))),
                    "lead_hours": h,
                    "model_hs_m": m,
                    "obs_hs_m": o,
                }
            )
    return ranges.with_app_sigma(pd.DataFrame(rows))


def test_fitted_two_tail_scale_puts_ten_percent_in_each_tail() -> None:
    train = _synthetic_pairs(120, bias=0.15, sd=0.25, seed=1)
    test = _synthetic_pairs(120, bias=0.15, sd=0.25, seed=2)
    app = ranges.evaluate_range(test, n_boot=50, by_station=False)
    # A biased model: the app range misses mostly above p90.
    pooled = app["table"][0]
    assert pooled["above_p90"] > 0.15 and pooled["below_p10"] < 0.05
    scale = ranges.fit_lead_scale(train)
    assert all(k_hi > k_lo for k_lo, k_hi in zip(scale.k_lo, scale.k_hi, strict=True))
    fitted = ranges.evaluate_range(test, scale, n_boot=50, by_station=False)
    for row in fitted["table"]:
        assert row["coverage"] == pytest.approx(0.80, abs=0.05)
        assert row["below_p10"] == pytest.approx(0.10, abs=0.04)
        assert row["above_p90"] == pytest.approx(0.10, abs=0.04)
        lo, hi = row["coverage_ci"]
        assert lo <= row["coverage"] <= hi
        assert row["coverage"] + row["below_p10"] + row["above_p90"] == pytest.approx(1.0)
    assert fitted["decision"]["passed"]
    symmetric = ranges.fit_symmetric_scale(train)
    assert symmetric.k_lo == symmetric.k_hi and symmetric.kind == "symmetric"


def test_coverage_decision_rules() -> None:
    def row(bucket: int | str, cov: float, n: int) -> dict[str, object]:
        return {"bucket": bucket, "coverage": cov, "n": n}

    table = [row("all", 0.5, 9999), row(24, 0.80, 500), row(48, 0.851, 500), row(240, 0.2, 199)]
    decision = ranges.coverage_decision(table)  # type: ignore[arg-type]
    assert decision["buckets_checked"] == [24, 48]  # "all" and n < 200 are not checked
    assert decision["failing"] == [{"bucket": 48, "coverage": 0.851, "n": 500}]
    assert not decision["passed"]
    assert ranges.coverage_decision([row(24, 0.75, 200), row(72, 0.85, 300)])["passed"]  # type: ignore[list-item]
    assert not ranges.coverage_decision([row(24, 0.8, 10)])["passed"]  # type: ignore[list-item]


def test_split_purges_the_gap() -> None:
    frame = _synthetic_pairs(40, bias=0.0, sd=0.2, seed=3)
    test_start = pd.Timestamp(START + timedelta(days=40))
    gap = timedelta(hours=312)
    train, test, guard = ranges.train_test_split(frame, test_start, gap)
    assert train["valid_time"].max() <= test_start - gap
    assert test["cycle_time"].min() >= test_start
    assert guard["guard"] == "train_valid_time_before_first_test_cycle_minus_gap"
    assert len(train) + len(test) < len(frame)  # rows inside the gap are dropped
    with pytest.raises(LeakageError):
        # a gap shorter than the training rows' reach into the test period
        from app.services.ml.dataset import assert_no_leakage

        assert_no_leakage(frame, test, gap)


def test_validation_with_a_check_set() -> None:
    frame = _synthetic_pairs(60, bias=0.1, sd=0.2, seed=4)
    check = _synthetic_pairs(30, bias=0.3, sd=0.2, seed=5).assign(station_code="synthetic:b")
    result = ranges.run_range_validation(
        frame,
        test_start=pd.Timestamp(START + timedelta(days=60)),
        gap=timedelta(hours=312),
        check=check,
        n_boot=30,
    )
    assert result["split"]["test_cycles"] == 30
    assert set(result["fitted_range_test"]["by_station"]) == {"synthetic:a"}
    # The check set has another bias: the TRAIN-fitted range does not transfer to it.
    assert result["check"]["fitted_range"]["table"][0]["above_p90"] > 0.2
    text = ranges.format_coverage(result["fitted_range_test"]["table"], "fitted")
    assert text.splitlines()[0] == "fitted" and "coverage %" in text


def test_other_leads_row_and_station_consistency() -> None:
    frame = _synthetic_pairs(40, bias=0.0, sd=0.2, seed=6)
    model = frame["model_hs_m"].to_numpy(dtype=float)
    lo, hi = ranges.bounds(model, frame["sigma_off"].to_numpy(dtype=float))
    table = ranges.coverage_table(frame, lo, hi, n_boot=20)
    assert [r["bucket"] for r in table] == ["all", 24, 48, 72, 120, 168, 240, "other"]
    lead = frame["lead_hours"]
    in_buckets = sum(r["n"] for r in table if isinstance(r["bucket"], int))
    assert table[-1]["n"] == int((lead >= 3).sum()) - in_buckets > 0  # e.g. 3-17 h, 33-39 h
    # The decision never looks at "all" or "other".
    table[-1]["coverage"] = 0.0
    assert ranges.coverage_decision(table)["buckets_checked"] == [24, 48, 72, 120, 168, 240]

    def row(cov: float, ci: tuple[float, float]) -> list[dict[str, object]]:
        return [{"bucket": "all", "n": 500, "coverage": cov, "coverage_ci": list(ci)}]

    out = ranges.station_consistency(
        {
            "synthetic:narrow": row(0.69, (0.67, 0.71)),
            "synthetic:wide": row(0.91, (0.89, 0.92)),
            "synthetic:edge": row(0.855, (0.84, 0.87)),  # CI overlaps the band
        }
    )
    assert out["synthetic:narrow"]["contradicts"] == "too_narrow"
    assert out["synthetic:wide"]["contradicts"] == "too_wide"
    assert out["synthetic:edge"]["contradicts"] is None


def test_station_consistency_checks_every_lead_bucket() -> None:
    """A station whose all-leads CI overlaps the band but whose CI at one lead bucket
    (n >= MIN_BUCKET_N) lies entirely outside it contradicts the range at that lead (the
    pattern of 46026 at 240 h); a bucket with too few pairs is not checked. SYNTHETIC
    coverage rows."""

    def r(bucket: object, cov: float, ci: tuple[float, float], n: int = 900) -> dict[str, object]:
        return {"bucket": bucket, "n": n, "coverage": cov, "coverage_ci": list(ci)}

    ok = [r("all", 0.80, (0.79, 0.81)), r(24, 0.80, (0.77, 0.83)), r(240, 0.76, (0.72, 0.80))]
    long_lead_narrow = [*ok[:2], r(240, 0.69, (0.64, 0.74))]
    mixed = [r("all", 0.80, (0.79, 0.81)), r(24, 0.90, (0.87, 0.93)), r(240, 0.69, (0.64, 0.74))]
    sparse = [*ok[:2], r(240, 0.40, (0.20, 0.60), n=ranges.MIN_BUCKET_N - 1)]
    out = ranges.station_consistency(
        {"s:ok": ok, "s:long": long_lead_narrow, "s:mixed": mixed, "s:sparse": sparse}
    )
    assert out["s:ok"]["contradicts"] is None and out["s:ok"]["contradicting"] == []
    assert out["s:long"]["contradicts"] == "too_narrow"
    assert out["s:long"]["coverage"] == 0.80  # the all-leads row is still reported
    assert out["s:long"]["contradicting"] == [
        {
            "bucket": 240,
            "n": 900,
            "coverage": 0.69,
            "coverage_ci": [0.64, 0.74],
            "verdict": "too_narrow",
        }
    ]
    assert out["s:mixed"]["contradicts"] == "mixed"
    assert [c["bucket"] for c in out["s:mixed"]["contradicting"]] == [24, 240]
    assert out["s:sparse"]["contradicts"] is None
