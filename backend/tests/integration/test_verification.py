"""Verification against the database: pairing, rolling metrics, backtests and the CLI.

All stations, observations and forecasts here are SYNTHETIC test rows (hand-written
values, or tests/synthetic_waves.py); station codes start with ``synthetic:``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.cli import main as cli_main
from app.core.config import get_settings
from app.models import (
    BuoyObservation,
    BuoyStation,
    ForecastAccuracyMetric,
    PointForecast,
    PredictionValidation,
)
from app.services.verification import metrics as vm
from app.services.verification.backtest import format_table, run_backtest
from app.services.verification.jobs import compute_accuracy_metrics, run_verification
from app.services.verification.pairing import pair_forecasts
from tests.synthetic_waves import insert_case, make_case

pytestmark = pytest.mark.integration

CYCLE = datetime(2026, 9, 1, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 20, 6, tzinfo=UTC)


def _station(db: Session, code: str = "synthetic:pair", **meta: Any) -> BuoyStation:
    station = BuoyStation(
        code=code,
        provider="synthetic",
        name="Synthetic pairing station",
        latitude=-30.0,  # invented test coordinates
        longitude=150.0,
        metadata_=meta,
    )
    db.add(station)
    db.flush()
    return station


def _obs(db: Session, station: BuoyStation, at: datetime, **values: Any) -> None:
    row = {"hs_kind": "spectral_hm0", "qc_flag": "good", "dataset": "synthetic", **values}
    db.add(BuoyObservation(station_id=station.id, observed_at=at, **row))


def _fc(db: Session, station: BuoyStation, lead: float, **values: Any) -> None:
    row = {
        "source_code": "synthetic_src",
        "family": "synthetic",
        "extra": {"period_kind": "tp", "direction_kind": "peak"},
        **values,
    }
    db.add(
        PointForecast(
            station_id=station.id,
            cycle_time=CYCLE,
            valid_time=CYCLE + timedelta(hours=lead),
            lead_hours=lead,
            **row,
        )
    )


def _pairs(db: Session, variable: str | None = None) -> list[PredictionValidation]:
    stmt = select(PredictionValidation).order_by(
        PredictionValidation.lead_hours, PredictionValidation.variable
    )
    if variable:
        stmt = stmt.where(PredictionValidation.variable == variable)
    return list(db.scalars(stmt))


def test_pairing_rules_and_idempotence(db: Session) -> None:
    st = _station(db)
    # Lead 6: nearest good obs is 10 min after (bad obs 2 min after is ignored).
    _fc(db, st, 6, hs_m=2.0, tp_s=12.0, direction_deg=350.0)
    _obs(db, st, CYCLE + timedelta(hours=6, minutes=2), hs_m=9.0, qc_flag="bad")
    _obs(
        db,
        st,
        CYCLE + timedelta(hours=6, minutes=10),
        hs_m=1.5,
        tp_s=11.0,
        peak_direction_deg=10.0,
        mean_direction_deg=30.0,
    )
    _obs(db, st, CYCLE + timedelta(hours=5, minutes=35), hs_m=1.7)  # 25 min: farther
    # Lead 12: only a time-domain H1/3 observation -> kept, flagged h_third.
    _fc(db, st, 12, hs_m=2.2, tp_s=12.0, direction_deg=200.0)
    _obs(db, st, CYCLE + timedelta(hours=12), hs_m=2.0, hs_kind="h_third")
    # Lead 18: nearest observation is 40 min away -> outside the 30 min tolerance.
    _fc(db, st, 18, hs_m=2.4)
    _obs(db, st, CYCLE + timedelta(hours=18, minutes=40), hs_m=2.1)
    # Lead 24: a mean period forecast must not be paired with a peak period.
    _fc(
        db,
        st,
        24,
        hs_m=2.6,
        tp_s=9.0,
        extra={"period_kind": "tm01", "direction_kind": "mean"},
        direction_deg=100.0,
    )
    _obs(
        db,
        st,
        CYCLE + timedelta(hours=24),
        hs_m=2.5,
        tp_s=13.0,
        peak_direction_deg=90.0,
        mean_direction_deg=95.0,
    )
    db.commit()

    summary = pair_forecasts(db, now=NOW, lookback=timedelta(days=30))
    assert summary.forecasts_scanned == 4
    hs = {p.lead_hours: p for p in _pairs(db, "hs")}
    assert set(hs) == {6.0, 12.0, 24.0}
    assert hs[6.0].observed_value == pytest.approx(1.5) and hs[6.0].error == pytest.approx(0.5)
    assert hs[6.0].observed_kind == "spectral_hm0"
    assert hs[12.0].observed_kind == "h_third"  # never silently mixed with Hm0
    assert summary.hs_by_kind == {"spectral_hm0": 2, "h_third": 1}
    assert summary.unmatched["hs"] == 1
    tp = _pairs(db, "tp")
    assert [(p.lead_hours, p.forecast_value, p.observed_value) for p in tp] == [(6.0, 12.0, 11.0)]
    assert summary.skipped["tp_period_not_peak_or_unknown"] == 1
    dirs = {p.lead_hours: p for p in _pairs(db, "dir")}
    # Forecast 350 vs observed 10 (both FROM): signed circular error -20, not 340.
    assert dirs[6.0].error == pytest.approx(-20.0) and dirs[6.0].observed_kind == "peak"
    # A mean-direction forecast is compared with the observed MEAN direction.
    assert dirs[24.0].observed_value == pytest.approx(95.0) and dirs[24.0].error == pytest.approx(
        5.0
    )

    count = db.scalar(select(func.count()).select_from(PredictionValidation))
    # Incremental: nothing new to pair (the unmatched lead-18 forecast is re-scanned).
    again = pair_forecasts(db, now=NOW, lookback=timedelta(days=30))
    assert again.forecasts_scanned == 1 and sum(again.pairs_written.values()) == 0
    # Idempotent: a full re-run rewrites exactly the same rows.
    full = pair_forecasts(db, now=NOW, full=True)
    assert full.forecasts_scanned == 4
    assert db.scalar(select(func.count()).select_from(PredictionValidation)) == count
    # A late observation for lead 18 is picked up incrementally.
    _obs(db, st, CYCLE + timedelta(hours=18, minutes=5), hs_m=2.0)
    db.commit()
    late = pair_forecasts(db, now=NOW, lookback=timedelta(days=30))
    assert late.pairs_written["hs"] == 1
    # An observation re-flagged as bad drops out on the next full run.
    db.execute(
        update(BuoyObservation)
        .where(BuoyObservation.observed_at == CYCLE + timedelta(hours=18, minutes=5))
        .values(qc_flag="bad")
    )
    db.commit()
    pair_forecasts(db, now=NOW, full=True)
    assert 18.0 not in {p.lead_hours for p in _pairs(db, "hs")}


def test_future_forecasts_are_not_paired(db: Session) -> None:
    st = _station(db)
    _fc(db, st, 6, hs_m=2.0)
    _obs(db, st, CYCLE + timedelta(hours=6), hs_m=2.0)
    db.commit()
    summary = pair_forecasts(db, now=CYCLE + timedelta(hours=3))
    assert summary.forecasts_scanned == 0


def test_run_verification_respects_the_flag(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "verification_enabled", False)
    assert "skipped" in run_verification(db)


@pytest.fixture
def synthetic_bias(db: Session) -> dict[str, int]:
    case = make_case("bias", n_stations=2, days=70, lead_step=12, max_lead=120, seed=21)
    return insert_case(db, case)


def test_run_verification_writes_rolling_metrics(
    db: Session, synthetic_bias: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "verification_enabled", True)
    now = datetime(2025, 3, 20, 7, tzinfo=UTC)  # synthetic case starts 2025-01-01
    backfill = pair_forecasts(db, now=now, full=True)  # what `verify pair --full` does
    assert backfill.pairs_written["hs"] > 1000
    result = run_verification(db, now=now)
    # The scheduled run only looks back DEFAULT_LOOKBACK and finds nothing new to pair.
    assert sum(result["pairing"]["pairs_written"].values()) == 0
    assert result["metrics"]["rows_upserted"] > 0
    assert result["metrics"]["corrected_model"] is None  # nothing active
    rows = list(db.scalars(select(ForecastAccuracyMetric)))
    scopes = {(r.scope, r.scope_key) for r in rows}
    assert ("station", "synthetic:station-1") in scopes
    assert ("region", "synthetic-east") in scopes and ("region", "synthetic-west") in scopes
    assert ("global", "all") in scopes
    assert {r.variable for r in rows} == {"hs", "tp", "dir"}
    assert {r.model_version for r in rows} == {"raw"}
    assert {(r.period_end - r.period_start).days for r in rows} == {30, 90}
    glob = next(
        r
        for r in rows
        if r.scope == "global"
        and r.variable == "hs"
        and r.lead_bucket_h == 120
        and (r.period_end - r.period_start).days == 30
    )
    # The synthetic model over-forecasts by 1.15-1.25x: positive bias, HH reported.
    assert glob.bias is not None and glob.bias > 0
    assert glob.hh is not None and glob.scatter_index is not None
    assert glob.skill_vs_persistence is not None
    assert glob.extra["rmse_ci"]["lower"] <= glob.rmse <= glob.extra["rmse_ci"]["upper"]
    assert set(glob.extra["events"]) == {"2", "3"}
    direction = next(r for r in rows if r.variable == "dir" and r.scope == "global")
    assert direction.extra["units"] == "deg" and direction.hh is None
    # Re-running on the same day upserts the same keys (no duplicates).
    before = len(rows)
    again = compute_accuracy_metrics(db, now=now)
    assert again["rows_upserted"] == before
    assert db.scalar(select(func.count()).select_from(ForecastAccuracyMetric)) == before


def test_backtest_and_cli(
    db: Session, synthetic_bias: dict[str, int], capsys: pytest.CaptureFixture[str]
) -> None:
    result = run_backtest(db, source="synthetic_gfswave", folds=3, n_boot=100)
    assert len(result.folds) >= 2
    assert result.gap_hours == pytest.approx(120 + 72)
    for fold in result.folds:
        assert (
            fold["leakage_guard"]["guard"] == "train_valid_time_before_first_test_cycle_minus_gap"
        )
    by_bucket = {r["lead_bucket_h"]: r for r in result.table}
    assert set(by_bucket) == {vm.ALL_LEADS_BUCKET, *vm.LEAD_BUCKETS_H}
    pooled = by_bucket[vm.ALL_LEADS_BUCKET]
    assert pooled["n"] > 0 and pooled["bias"] > 0
    assert pooled["rmse_ci"][0] <= pooled["rmse"] <= pooled["rmse_ci"][1]
    assert pooled["skill_vs_persistence"] is not None
    assert by_bucket[168]["n"] == 0  # synthetic leads stop at 120 h
    assert result.metrics_written > 0
    versions = set(db.scalars(select(ForecastAccuracyMetric.model_version).distinct()))
    assert versions == {"raw", "persistence", "climatology"}
    text = format_table(result)
    assert "RMSE" in text and "SS_pers" in text and "favour negatively biased" in text

    assert (
        cli_main(
            [
                "verify",
                "backtest",
                "--source",
                "synthetic_gfswave",
                "--folds",
                "2",
                "--boot",
                "50",
                "--no-write",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Backtest synthetic_gfswave" in out and "metrics rows written: 0" in out
    assert cli_main(["verify", "backtest", "--source", "nope", "--no-write"]) == 1
    # The synthetic buoys report spectral Hm0 only: an H1/3 backtest finds nothing to
    # verify instead of silently using the Hm0 observations.
    assert (
        cli_main(["verify", "backtest", "--source", "synthetic_gfswave", "--hs-kind", "h_third"])
        == 1
    )
    assert "h_third observations" in capsys.readouterr().err
    assert cli_main(["verify", "pair", "--full"]) == 0
    assert '"forecasts_scanned"' in capsys.readouterr().out
    assert cli_main(["verify", "metrics", "--days", "30"]) == 0
    assert '"rows_upserted"' in capsys.readouterr().out
