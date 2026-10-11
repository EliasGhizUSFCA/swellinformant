"""GEFS-Wave ensemble statistics: layout checks, partition order, units, missing values,
member quantiles and run discovery. All GRIB data here is SYNTHETIC (tests/unit/gefs_synthetic)."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest

from app.core.config import Settings, get_settings
from app.services.forecasts.base import RunInfo, SpotPoint
from app.services.forecasts.ensemble import (
    MEMBERS,
    MIN_MEMBERS_FOR_QUANTILES,
    PUBLISHED_STEPS,
    GefsLayoutError,
    GefsWaveEnsembleProvider,
    check_partition_order,
    cv_rules_out_zero_fill,
    forecast_steps,
    member_quantiles,
    select_stat_records,
    swell1_complete_certain,
)
from app.services.http import TransientProviderError
from tests.unit.gefs_synthetic import (
    BASE_URL,
    CYCLE,
    PARAMS,
    FakeBucket,
    Grid,
    Record,
    publish_cycle,
    stat_records,
)

# The live index of https://noaa-gefs-pds.s3.amazonaws.com/gefs.20261010/12/wave/gridded/
# gefs.wave.t12z.mean.global.0p25.f168.grib2.idx (NOAA open data, fetched 2026-10-10).
LIVE_MEAN_IDX = """\
1:0:d=2026101012:HTSGW:surface:168 hour fcst:ens mean
2:354510:d=2026101012:PERPW:surface:168 hour fcst:ens mean
3:876997:d=2026101012:DIRPW:surface:168 hour fcst:ens mean
4:1729848:d=2026101012:WVHGT:surface:168 hour fcst:ens mean
5:2238639:d=2026101012:WVPER:surface:168 hour fcst:ens mean
6:2908326:d=2026101012:WVDIR:surface:168 hour fcst:ens mean
7:3925324:d=2026101012:WIND:surface:168 hour fcst:ens mean
8:4468236:d=2026101012:WDIR:surface:168 hour fcst:ens mean
9:5368219:d=2026101012:SWELL:surface:168 hour fcst:ens mean
10:5839529:d=2026101012:SWELL:surface:168 hour fcst:ens mean
11:6273357:d=2026101012:SWPER:surface:168 hour fcst:ens mean
12:6915902:d=2026101012:SWPER:surface:168 hour fcst:ens mean
"""

# Small synthetic regional grid: rows 10N..5N, columns 10W..5W, 1 degree.
SMALL = Grid(10.0, 350.0, 1.0, 6, 6)
SPOT = SpotPoint(1, "alpha", 8.2, -7.9, 8.0, -8.0)  # forecast point on cell (8N, 8W)


def make_settings(**overrides: object) -> Settings:
    values: dict[str, Any] = {
        "noaa_gefs_base_url": BASE_URL,
        "gefs_max_lead_hours": 24,
        "gefs_step_hours": 12,
        "grib_max_cell_distance_km": 150.0,
        "grib_download_workers": 2,
        **overrides,
    }
    return get_settings().model_copy(update=values)


def make_provider(
    bucket: FakeBucket, members: bool = False, **settings: object
) -> GefsWaveEnsembleProvider:
    return GefsWaveEnsembleProvider(
        make_settings(**settings), client=bucket.client(), members=members, backoff_seconds=0
    )


RUN = RunInfo("gefs-wave:2026101012", CYCLE, wave_model_run_at=CYCLE)


# --------------------------------------------------------------------------- layout
def test_forecast_steps_skip_steps_noaa_never_publishes() -> None:
    assert len(PUBLISHED_STEPS) == 105  # 3-hourly to f240, 6-hourly to f384
    published, skipped = forecast_steps(240, 6)
    assert published == list(range(0, 241, 6)) and skipped == []
    published, skipped = forecast_steps(384, 9)
    assert 243 in skipped and 261 in skipped and 279 in skipped
    assert 252 in published and 270 in published and 234 in published
    assert forecast_steps(384, 12)[1] == []


def test_partition_one_is_the_first_swell_record_of_the_live_layout() -> None:
    picked = select_stat_records(LIVE_MEAN_IDX, "mean", with_swell2=True)
    assert picked["swell1_height"].number == 9
    assert picked["swell2_height"].number == 10
    assert picked["swell1_period"].number == 11
    assert [picked[k].number for k in ("hs", "tp", "wind")] == [1, 2, 7]
    assert picked["swell1_height"].length == 5839529 - 5368219


def _edit_idx(drop: int | None = None, insert: str | None = None, desc: str | None = None) -> str:
    lines = LIVE_MEAN_IDX.splitlines()
    if drop is not None:
        lines = [ln for ln in lines if not ln.startswith(f"{drop}:")]
    if insert is not None:
        lines.append(insert)
    if desc is not None:
        lines = [ln.replace("ens mean", desc) for ln in lines]
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    "text",
    [
        _edit_idx(drop=10),  # only one SWELL record: which partition is it?
        _edit_idx(insert="13:7500000:d=2026101012:SWELL:surface:168 hour fcst:ens mean"),
        _edit_idx(insert="13:7500000:d=2026101012:HTSGW:surface:168 hour fcst:ens mean"),
        _edit_idx(drop=7),  # WIND missing
        _edit_idx(desc="ens spread"),  # a spread file served under the mean name
        LIVE_MEAN_IDX.replace(
            "10:5839529:d=2026101012:SWELL:surface", "10:5839529:d=2026101012:SWELL:2 in sequence"
        ),
        "",
    ],
)
def test_layout_changes_fail_loudly(text: str) -> None:
    with pytest.raises(GefsLayoutError):
        select_stat_records(text, "mean")


def test_explicit_partition_labels_are_used_when_present() -> None:
    text = LIVE_MEAN_IDX.replace(
        "9:5368219:d=2026101012:SWELL:surface", "9:5368219:d=2026101012:SWELL:2 in sequence"
    ).replace(
        "10:5839529:d=2026101012:SWELL:surface", "10:5839529:d=2026101012:SWELL:1 in sequence"
    )
    picked = select_stat_records(text, "mean", with_swell2=True)
    assert picked["swell1_height"].number == 10 and picked["swell2_height"].number == 9
    assert picked["swell1_period"].number == 11  # SWPER still coded "surface": by order


def test_partition_order_check() -> None:
    a = np.array([3.0, 2.0, np.nan, 1.0])
    b = np.array([1.0, 2.005, 0.5, np.nan])
    assert check_partition_order(a, b) == {"verified": True, "cells": 2, "fraction": 1.0}
    with pytest.raises(GefsLayoutError):
        check_partition_order(b[:2], a[:2] + 1)
    assert check_partition_order(np.array([np.nan]), np.array([1.0]))["verified"] is False


# --------------------------------------------------------------------------- reading
def test_fetch_reads_partition_one_by_order_and_converts_units() -> None:
    bucket = FakeBucket()

    def stats(kind: str, fhour: int) -> list[Record]:
        over = {"SWELL": [2.0, 1.0], "SWPER": [14.0, 8.0]}  # partition 1 differs from 2
        if kind == "spread":
            over = {"SWELL": [0.3, 0.9], "SWPER": [0.5, 3.0]}
        return stat_records(kind, SMALL, overrides=over)

    publish_cycle(bucket, SMALL, [0, 12, 24], stats=stats)
    res = make_provider(bucket).fetch_stats([SPOT], RUN)
    assert res.errors == {} and res.steps_failed == {}
    rows = res.rows[1]
    assert [r["lead_hours"] for r in rows] == [0.0, 12.0, 24.0]
    assert [r["valid_time"] for r in rows] == [CYCLE + timedelta(hours=h) for h in (0, 12, 24)]
    r = rows[1]
    assert r["members"] == 31
    assert r["hs_mean_m"] == pytest.approx(2.5, abs=1e-3)
    assert r["hs_spread_m"] == pytest.approx(0.4, abs=1e-3)
    assert r["tp_mean_s"] == pytest.approx(12.0, abs=1e-3)
    assert r["swell1_height_mean_m"] == pytest.approx(2.0, abs=1e-3)  # first record, not 1.0
    assert r["swell1_height_spread_m"] == pytest.approx(0.3, abs=1e-3)
    assert r["swell1_period_mean_s"] == pytest.approx(14.0, abs=1e-3)
    assert r["swell1_period_spread_s"] == pytest.approx(0.5, abs=1e-3)
    assert r["wind_speed_mean_kmh"] == pytest.approx(36.0, abs=0.01)  # 10 m/s
    assert r["wind_speed_spread_kmh"] == pytest.approx(7.2, abs=0.01)  # 2 m/s
    assert r["extra"]["period_kind"] == "tp"
    assert r["extra"]["swell1_complete_certain"] is True  # 0.5 / 14 s is far below 0.183
    assert res.checks["partition_order"]["verified"] is True
    assert res.checks["members_in_stats"] == [31]
    assert res.cells[1]["basis"] == "forecast_point" and res.cells[1]["distance_km"] == 0.0
    # 3 steps x (2 .idx + 8 ranges) = 30 GETs, minus one on the first step, where the extra
    # partition-2 record joins partition 1 and the first SWPER into one contiguous range.
    assert res.request_count == len(bucket.gets()) == 29
    assert res.bytes_downloaded > 0


def test_swapped_partition_order_aborts_the_run() -> None:
    bucket = FakeBucket()
    publish_cycle(
        bucket,
        SMALL,
        [0, 12, 24],
        stats=lambda kind, f: stat_records(
            kind, SMALL, overrides={"SWELL": [1.0, 3.0]} if kind == "mean" else None
        ),
    )
    with pytest.raises(GefsLayoutError, match="partition order"):
        make_provider(bucket).fetch_stats([SPOT], RUN)


def test_records_must_decode_to_the_expected_parameter_and_statistic() -> None:
    def wrong_param(kind: str, fhour: int) -> list[Record]:
        recs = stat_records(kind, SMALL)
        recs[0].param = PARAMS["WIND"]  # the .idx says HTSGW
        return recs

    bucket = FakeBucket()
    publish_cycle(bucket, SMALL, [0, 12, 24], stats=wrong_param)
    with pytest.raises(GefsLayoutError, match="GRIB2 parameter"):
        make_provider(bucket).fetch_stats([SPOT], RUN)

    def mean_in_spread_file(kind: str, fhour: int) -> list[Record]:
        recs = stat_records(kind, SMALL)
        for rec in recs:
            rec.derived = 0
        return recs

    bucket = FakeBucket()
    publish_cycle(bucket, SMALL, [0, 12, 24], stats=mean_in_spread_file)
    with pytest.raises(GefsLayoutError, match="derivedForecast"):
        make_provider(bucket).fetch_stats([SPOT], RUN)


def test_unreadable_or_inconsistent_steps_are_reported_gaps() -> None:
    def stale_step(kind: str, fhour: int) -> list[Record]:
        recs = stat_records(kind, SMALL)
        if fhour == 24 and kind == "spread":
            recs[0].message_step = 18  # file content does not match its name
        return recs

    bucket = FakeBucket()
    publish_cycle(bucket, SMALL, [0, 12, 24], stats=stale_step)
    bucket.status[bucket.key(CYCLE, "spread", 12) + ".idx"] = 404
    res = make_provider(bucket).fetch_stats([SPOT], RUN)
    assert sorted(res.steps_failed) == ["f012", "f024"]
    assert "not published" in res.steps_failed["f012"]
    assert "step 18" in res.steps_failed["f024"]
    assert [r["lead_hours"] for r in res.rows[1]] == [0.0]  # mean without spread is dropped
    assert any("2 of 3 steps unreadable" in w for w in res.warnings)


def test_transient_failure_of_every_first_step_raises_transient() -> None:
    bucket = FakeBucket()
    publish_cycle(bucket, SMALL, [0, 12, 24])
    for f in (0, 12, 24):
        bucket.status[bucket.key(CYCLE, "mean", f) + ".idx"] = 503
    with pytest.raises(TransientProviderError, match="none of the first 3"):
        make_provider(bucket).fetch_stats([SPOT], RUN)


def test_missing_values_pick_nearest_valid_cell_then_break_location() -> None:
    land = np.zeros((SMALL.nj, SMALL.ni), dtype=bool)
    land[2, 2] = True  # 9999 (bitmap-masked) at the alpha forecast point's own cell
    bucket = FakeBucket()
    publish_cycle(bucket, SMALL, [0, 12, 24], land=land)
    spots = [
        SPOT,
        SpotPoint(2, "beta", 9.0, -6.0, 30.0, -8.0),  # forecast point outside the grid
        SpotPoint(3, "gamma", 40.0, 40.0, 40.0, 40.0),  # nowhere near the grid
    ]
    res = make_provider(bucket).fetch_stats(spots, RUN)
    alpha = res.cells[1]
    assert alpha["basis"] == "forecast_point"
    assert (alpha["lat"], alpha["lon"]) != (8.0, -8.0)
    assert alpha["distance_km"] == pytest.approx(110.1, abs=0.2)  # one column away at 8N
    assert res.cells[2] == {"lat": 9.0, "lon": -6.0, "distance_km": 0.0, "basis": "break"}
    assert 3 not in res.rows and "no valid GEFS-Wave cell" in res.errors[3]
    assert res.rows[1][0]["hs_mean_m"] == pytest.approx(2.5, abs=1e-3)


def test_cell_masked_at_a_later_step_is_skipped_and_reported() -> None:
    ice = np.zeros((SMALL.nj, SMALL.ni), dtype=bool)
    ice[2, 2] = True

    def stats(kind: str, fhour: int) -> list[Record]:
        return stat_records(kind, SMALL, land=ice if fhour == 12 else None)

    bucket = FakeBucket()
    publish_cycle(bucket, SMALL, [0, 12, 24], stats=stats)
    res = make_provider(bucket).fetch_stats([SPOT], RUN)
    assert [r["lead_hours"] for r in res.rows[1]] == [0.0, 24.0]
    assert any("alpha: 1 of 3 steps masked" in w for w in res.warnings)


def test_geometry_change_within_a_run_is_a_layout_error() -> None:
    other = Grid(10.0, 350.0, 0.5, 11, 11)

    def stats(kind: str, fhour: int) -> list[Record]:
        return stat_records(kind, other if fhour == 24 else SMALL)

    bucket = FakeBucket()
    for f in (0, 12, 24):
        for kind in ("mean", "spread"):
            bucket.put_file(CYCLE, kind, f, stats(kind, f), other if f == 24 else SMALL)
    with pytest.raises(GefsLayoutError, match="geometry"):
        make_provider(bucket).fetch_stats([SPOT], RUN)


# --------------------------------------------------------------------------- members
def test_member_mode_stores_quantiles_and_member_count() -> None:
    bucket = FakeBucket()
    hs = {m: 1.0 + 0.1 * k for k, m in enumerate(MEMBERS)}  # 1.0 .. 4.0 m, mean 2.5 m
    publish_cycle(bucket, SMALL, [0, 12, 24], member_hs=lambda m, f: hs[m])
    res = make_provider(bucket, members=True).fetch_stats([SPOT], RUN)
    extra = res.rows[1][1]["extra"]
    expected = np.quantile(np.array(list(hs.values())), [0.1, 0.5, 0.9])
    assert extra["hs_members"] == 31
    assert extra["hs_p10_m"] == pytest.approx(expected[0], abs=2e-3)
    assert extra["hs_p50_m"] == pytest.approx(expected[1], abs=2e-3)
    assert extra["hs_p90_m"] == pytest.approx(expected[2], abs=2e-3)
    assert extra["hs_quantile_method"] == "linear"
    assert extra["hs_members_mean_m"] == pytest.approx(2.5, abs=2e-3)
    assert res.checks["member_mean_vs_published_max_diff_m"] <= 0.002
    # Stats: 29 GETs; members: 31 files x (.idx + one HTSGW range) x 3 steps.
    assert res.request_count == 29 + 31 * 2 * 3


def test_member_mode_reports_unpublished_members() -> None:
    bucket = FakeBucket()
    publish_cycle(
        bucket,
        SMALL,
        [0, 12, 24],
        member_hs=lambda m, f: None if (m in ("p29", "p30") and f == 12) else 2.0,
    )
    res = make_provider(bucket, members=True).fetch_stats([SPOT], RUN)
    by_lead = {r["lead_hours"]: r["extra"] for r in res.rows[1]}
    assert by_lead[12.0]["hs_members"] == 29 and "hs_p90_m" in by_lead[12.0]
    assert by_lead[0.0]["hs_members"] == 31
    assert any("f012: 2 of 31 member files unreadable" in w for w in res.warnings)
    assert res.steps_failed == {}  # missing members reduce the count; the step is kept


def test_member_quantiles() -> None:
    vals = [float(v) for v in np.linspace(0.5, 3.5, 31)]
    q = member_quantiles(vals)
    assert q["hs_members"] == 31
    assert q["hs_p10_m"] == pytest.approx(float(np.quantile(vals, 0.1)), abs=1e-3)
    assert q["hs_p50_m"] == pytest.approx(2.0, abs=1e-3)
    assert q["hs_members_std_m"] == pytest.approx(float(np.std(vals)), abs=1e-3)
    few = member_quantiles(vals[: MIN_MEMBERS_FOR_QUANTILES - 1])
    assert "hs_p10_m" not in few and few["hs_members"] == MIN_MEMBERS_FOR_QUANTILES - 1
    assert member_quantiles([]) == {"hs_members": 0}


def _published(values: list[float]) -> tuple[float, float]:
    """Zero-filled mean and population spread, rounded like the 0.01-packed GRIB values."""
    arr = np.asarray(values)
    return round(float(arr.mean()), 2), round(float(arr.std(ddof=0)), 2)


def test_zero_fill_bound_per_pair() -> None:
    full = _published([14.0 + 0.1 * (k % 5) for k in range(31)])
    one_missing = _published([14.0] * 30 + [0.0])  # zero-filled member: CV = sqrt(1/30)
    kw: dict[str, Any] = {"members": 31, "mean_half_step": 0.005, "spread_half_step": 0.005}
    assert cv_rules_out_zero_fill(*full, **kw) is True
    assert cv_rules_out_zero_fill(*one_missing, **kw) is False
    assert math.isclose(one_missing[1] / one_missing[0], math.sqrt(1 / 30), rel_tol=1e-2)
    assert cv_rules_out_zero_fill(None, 0.1, **kw) is None
    assert cv_rules_out_zero_fill(0.0, 0.0, **kw) is False


def test_swell1_certificate_uses_height_or_period() -> None:
    half = {
        f"{c}:{k}": 0.005 for c in ("swell1_height", "swell1_period") for k in ("mean", "spread")
    }

    def values(heights: list[float], periods: list[float]) -> dict[str, float | None]:
        (hm, hs), (tm, ts) = _published(heights), _published(periods)
        return {
            "swell1_height_mean_m": hm,
            "swell1_height_spread_m": hs,
            "swell1_period_mean_s": tm,
            "swell1_period_spread_s": ts,
        }

    # Members agree on the height but partition 1 alternates between a 14 s and a 9 s
    # swell: the period pair proves nothing, the height pair proves completeness.
    rank_swap = values([1.05 + 0.002 * k for k in range(31)], [14.0] * 20 + [9.0] * 11)
    assert swell1_complete_certain(rank_swap, half, members=31) is True
    # One member without the partition (zero-filled): neither pair can prove completeness.
    missing = values([1.0] * 30 + [0.0], [14.0] * 30 + [0.0])
    assert swell1_complete_certain(missing, half, members=31) is False
    assert swell1_complete_certain({}, half, members=31) is None


# --------------------------------------------------------------------------- run discovery
def test_current_run_probes_mean_idx_and_falls_back_to_older_cycle() -> None:
    bucket = FakeBucket()
    older = CYCLE - timedelta(hours=6)
    bucket.objects[bucket.key(older, "mean", 24) + ".idx"] = b"1:0:d=x:HTSGW:surface:anl:ens mean\n"
    provider = make_provider(bucket, gefs_availability_lag_hours=7.5)
    run = provider.current_run(datetime(2026, 10, 10, 20, 0, tzinfo=UTC))  # nominal 12Z
    assert run.run_key == "gefs-wave:2026101006"
    assert run.issued_at == older and run.wave_model_run_at == older
    heads = [k for m, k, _ in bucket.log if m == "HEAD"]
    assert heads == [bucket.key(CYCLE, "mean", 24) + ".idx", bucket.key(older, "mean", 24) + ".idx"]
    assert provider.probe_requests == 2


def test_current_run_without_any_complete_cycle_is_transient() -> None:
    bucket = FakeBucket()
    with pytest.raises(TransientProviderError, match="no GEFS-Wave cycle"):
        make_provider(bucket).current_run(datetime(2026, 10, 10, 20, 0, tzinfo=UTC))
    assert len(bucket.log) == 4  # nominal + 3 older cycles


def test_provider_refuses_the_wave_forecast_path() -> None:
    provider = make_provider(FakeBucket())
    info = provider.source_info()
    assert (info.code, info.family, info.kind) == ("noaa_gefs_wave", "ncep_gefs", "ensemble")
    with pytest.raises(TypeError):
        provider.fetch([SPOT], RUN)


# --------------------------------------------------------------------------- CLI
def test_engine_cli_runs_the_ensemble_job(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[bool, bool]] = []

    def fake(
        db: object, now: object = None, force: bool = False, *, members: bool = False
    ) -> dict[str, object]:
        calls.append((force, members))
        return {"status": "success", "records": 3}

    monkeypatch.setattr("app.services.forecasts.ensemble.acquire_ensemble", fake)
    from app.cli import main

    assert main(["engine", "ensemble", "--force", "--members"]) == 0
    assert calls == [(True, True)]
    assert json.loads(capsys.readouterr().out) == {"status": "success", "records": 3}

    monkeypatch.setattr(
        "app.services.forecasts.ensemble.acquire_ensemble",
        lambda db, **kw: {"status": "failed", "message": "x"},
    )
    assert main(["engine", "ensemble"]) == 1
    with pytest.raises(SystemExit):
        main(["engine"])  # a nested subcommand is required
