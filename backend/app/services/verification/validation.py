"""Database runners for the buoy validation of the 80 % ranges and the confidence classes.

``verify ranges`` and ``verify confidence-classes`` (docs/ml-evaluation.md section 12):
both read the archived point forecasts of one deterministic source paired with spectral
Hm0 observations (``ml.dataset.build_dataset``: nearest observation within 30 min, leads
>= 3 h), split them chronologically (TEST = cycles from ``test_start``; TRAIN = valid
times at or before ``test_start - gap``, gap = max lead + 3 days) and hand the frames to
the pure functions in ``verification.ranges`` and ``verification.confidence_classes``.
With ``ensemble_source`` the pairs are restricted to hours that have an ensemble row of
the same station, cycle and valid time.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.services.ml.dataset import InsufficientDataError, build_dataset, required_gap
from app.services.verification import confidence_classes as cc
from app.services.verification import ranges
from app.services.verification.metrics import DEFAULT_BOOTSTRAP_SEED, DEFAULT_N_BOOT
from app.services.verification.pairing import load_point_forecasts


def load_pairs(
    db: Session,
    *,
    source: str,
    station_codes: Sequence[str],
    start: datetime | None = None,
    hs_kind: str = "spectral_hm0",
) -> pd.DataFrame:
    frame = build_dataset(
        db, sources=[source], station_codes=list(station_codes), start=start, hs_kinds=(hs_kind,)
    ).frame
    if frame.empty:
        raise InsufficientDataError(
            f"no {source} forecasts paired with {hs_kind} observations at {list(station_codes)}"
        )
    return frame


def with_ensemble(db: Session, frame: pd.DataFrame, ensemble_source: str) -> pd.DataFrame:
    ens = load_point_forecasts(
        db,
        station_ids=sorted({int(s) for s in frame["station_id"]}),
        sources=[ensemble_source],
        cycle_from=pd.Timestamp(frame["cycle_time"].min()).to_pydatetime(),
    )
    if ens.empty:
        raise InsufficientDataError(f"no {ensemble_source} rows at these stations")
    out = ranges.attach_ensemble(frame, ens)
    if out.empty:
        raise InsufficientDataError(f"no pair has a {ensemble_source} row at its valid time")
    return out


def _summary(frame: pd.DataFrame) -> dict[str, Any]:
    return {
        "rows": len(frame),
        "stations": sorted(frame["station_code"].unique().tolist()),
        "cycles": int(frame["cycle_time"].nunique()),
        "cycle_start": pd.Timestamp(frame["cycle_time"].min()).isoformat(),
        "cycle_end": pd.Timestamp(frame["cycle_time"].max()).isoformat(),
    }


def range_validation(
    db: Session,
    *,
    station_codes: Sequence[str],
    test_start: datetime,
    source: str = "gfswave_bulletin",
    ensemble_source: str | None = None,
    check_station_codes: Sequence[str] | None = None,
    start: datetime | None = None,
    n_boot: int = DEFAULT_N_BOOT,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Coverage of the app's offshore range on TEST, the TRAIN-fitted scale, and a check."""
    frame = load_pairs(db, source=source, station_codes=station_codes, start=start)
    gap = required_gap(float(frame["lead_hours"].max()))
    use_ens = ensemble_source is not None
    if ensemble_source is not None:
        frame = with_ensemble(db, frame, ensemble_source)
    frame = ranges.with_app_sigma(frame, ensemble=use_ens)
    check = None
    if check_station_codes:
        check = load_pairs(db, source=source, station_codes=check_station_codes)
        if ensemble_source is not None:
            check = with_ensemble(db, check, ensemble_source)
        check = ranges.with_app_sigma(check, ensemble=use_ens)
    result = ranges.run_range_validation(
        frame, test_start=pd.Timestamp(test_start), gap=gap, check=check, n_boot=n_boot, seed=seed
    )
    return {
        "source": source,
        "ensemble_source": ensemble_source,
        "range": "ensemble_heuristic" if use_ens else "heuristic (lead table)",
        "data": _summary(frame),
        "gap_hours": gap.total_seconds() / 3600.0,
        "n_boot": n_boot,
        "seed": seed,
        **result,
    }


def class_validation(
    db: Session,
    *,
    station_codes: Sequence[str],
    test_start: datetime,
    source: str = "gfswave_bulletin",
    ensemble_source: str | None = None,
    start: datetime | None = None,
    window_days: int = cc.WINDOW_DAYS,
    method: str = cc.METHOD,
    scan_boundaries: bool = False,
    n_boot: int = DEFAULT_N_BOOT,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Forecast-confidence classes (``method``) of the archived pairs and their realised
    errors, without (V0) and with (V1) causal verification rows. ``scan_boundaries`` adds
    the TRAIN-only boundary scan (single-source hours only, so not with an ensemble)."""
    if scan_boundaries and ensemble_source is not None:
        raise ValueError("the boundary scan is for single-source hours (no --ensemble-source)")
    pairs = load_pairs(db, source=source, station_codes=station_codes, start=start)
    gap = required_gap(float(pairs["lead_hours"].max()))
    use_ens = ensemble_source is not None
    frame = with_ensemble(db, pairs, ensemble_source) if ensemble_source is not None else pairs
    train, test, guard = ranges.train_test_split(frame, pd.Timestamp(test_start), gap)
    # The job's verification rows use every raw pair of the station before the cycle.
    verification = cc.causal_verification(
        pairs, cc.window_cycles(pd.concat([train, test])), window_days=window_days
    )
    out: dict[str, Any] = {
        "source": source,
        "ensemble_source": ensemble_source,
        "method": method,
        "data": _summary(frame),
        "split": guard,
        "verification_window_days": window_days,
        "verification_rows": len(verification),
        "n_boot": n_boot,
        "seed": seed,
        "variants": {},
    }
    for name, ver in (("V0_no_verification", None), ("V1_causal_verification", verification)):
        variant: dict[str, Any] = {}
        for label, part in (("train", train), ("test", test)):
            classes, rules = cc.classify_frame(part, ver, ensemble=use_ens, method=method)
            variant[label] = cc.evaluate_classes(part, classes, n_boot=n_boot, seed=seed)
            fired: dict[str, int] = {}
            for rs in rules:
                for r in rs:
                    fired[r] = fired.get(r, 0) + 1
            variant[label]["rules_fired"] = dict(sorted(fired.items()))
            if ver is not None and label == "test":
                variant[label]["verification_poor_effect"] = cc.rule_effect_by_bucket(
                    part, rules, "verification_poor", n_boot=n_boot, seed=seed
                )
        out["variants"][name] = variant
    if scan_boundaries:
        out["boundary_scan"] = cc.boundary_scan(train, test, n_boot=n_boot, seed=seed)
    return out


def format_class_table(result: dict[str, Any]) -> str:
    lines = [f"method {result.get('method', cc.METHOD)}"]
    for name, variant in result["variants"].items():
        for label in ("train", "test"):
            ev = variant[label]
            lines.append(f"{name} {label}: counts {ev['counts']}")
            for cls, sc in ev["scores"].items():
                if not sc.get("n"):
                    continue
                lines.append(
                    f"  {cls:>8} n={sc['n']:>7} bias={sc['bias']:+.3f} "
                    f"med|e|={sc['median_abs_error']:.3f} p90|e|={sc['p90_abs_error']:.3f} "
                    f"RMSE={sc['rmse']:.3f} [{sc['rmse_ci'][0]:.3f},{sc['rmse_ci'][1]:.3f}]"
                )
            lines.append(f"  monotonicity: {ev['monotonicity']}")
            if "monotonicity_present_classes" in ev:
                lines.append(
                    f"  monotonicity (no High; Moderate < Low): "
                    f"{ev['monotonicity_present_classes']}"
                )
    scan = result.get("boundary_scan")
    if scan:
        lines.append(
            f"boundary scan (TRAIN, current boundary {scan['current_boundary_h']} h): "
            f"passing {scan['passing_on_train']}, choice {scan['choice']}"
        )
        if "test" in scan:
            lines.append(f"  choice on TEST: {scan['test']['monotonicity']}")
    return "\n".join(lines)
