"""``python -m app.cli verify ...``: pairing, rolling metrics and backtests.

  verify pair [--full] [--source S] [--lookback-days N]
  verify metrics [--days N]
  verify backtest --source S [--start ISO --end ISO] [--folds K] [--station CODE]
                  [--hs-kind spectral_hm0|h_third] [--boot N] [--no-write] [--json]
                  [--lt-climatology-before ISO] [--by-season]
  verify ranges --station CODE ... --test-start ISO [--source S] [--ensemble-source E]
                [--check-station CODE ...] [--start ISO] [--boot N] [--json PATH]
  verify confidence-classes --station CODE ... --test-start ISO [--source S]
                [--ensemble-source E] [--start ISO] [--window-days N] [--boot N]
                [--method forecast-v1|candidate-c2] [--scan-boundaries] [--json PATH]

``ranges`` scores the coverage of the app's offshore 80 % range against buoys and fits
the lead-dependent scale on the TRAIN period; ``confidence-classes`` scores the realised
error of the forecast-confidence classes (the current forecast-v1, or the candidate C2
that was evaluated and not adopted) and optionally runs the TRAIN-only boundary scan
(docs/ml-evaluation.md section 12).

These commands run whether or not VERIFICATION_ENABLED is set (that flag only gates
the scheduled job).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any


def _iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser("verify", help="forecast verification and backtests")
    sub = parser.add_subparsers(dest="verify_cmd", required=True)
    pair = sub.add_parser("pair", help="pair point forecasts with observations")
    pair.add_argument("--full", action="store_true", help="reprocess every forecast")
    pair.add_argument("--source", action="append", default=None)
    pair.add_argument("--lookback-days", type=float, default=None)
    metrics = sub.add_parser("metrics", help="recompute rolling accuracy metrics")
    metrics.add_argument("--days", type=int, default=None, help="one window of N days")
    backtest = sub.add_parser("backtest", help="rolling-origin backtest of one source")
    backtest.add_argument("--source", required=True)
    backtest.add_argument("--start", type=_iso, default=None, help="first cycle (ISO 8601)")
    backtest.add_argument("--end", type=_iso, default=None, help="last cycle (ISO 8601)")
    backtest.add_argument("--folds", type=int, default=5)
    backtest.add_argument("--station", action="append", default=None)
    backtest.add_argument(
        "--hs-kind",
        default="spectral_hm0",
        choices=("spectral_hm0", "h_third", "unknown"),
        help="observed height definition to verify against (default: spectral Hm0)",
    )
    backtest.add_argument("--boot", type=int, default=1000, help="bootstrap replicates")
    backtest.add_argument(
        "--lt-climatology-before",
        type=_iso,
        default=None,
        help="add a long-term climatology reference from QC'd observations before ISO "
        "(must be at or before the first tested cycle)",
    )
    backtest.add_argument(
        "--by-season",
        action="store_true",
        help="also report winter (Oct-Mar, northern hemisphere) and summer half-years",
    )
    backtest.add_argument("--no-write", action="store_true")
    backtest.add_argument("--json", action="store_true", help="print JSON instead of a table")
    for name, help_text in (
        ("ranges", "coverage of the app's offshore 80 % range at buoys"),
        ("confidence-classes", "realised error of the forecast-confidence classes"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--station", action="append", required=True)
        p.add_argument("--test-start", type=_iso, required=True, help="first TEST cycle")
        p.add_argument("--source", default="gfswave_bulletin")
        p.add_argument("--ensemble-source", default=None, help="e.g. gefswave_station")
        p.add_argument("--start", type=_iso, default=None, help="first cycle read")
        p.add_argument("--boot", type=int, default=1000, help="bootstrap replicates")
        p.add_argument("--json", default=None, help="write the full result to PATH")
        if name == "ranges":
            p.add_argument(
                "--check-station",
                action="append",
                default=None,
                help="also score these stations with the TRAIN-fitted scale (never fitted on)",
            )
        else:
            # forecast_confidence is a light, pure module (confidence_classes needs pandas).
            from app.services.surf_quality.forecast_confidence import METHOD

            p.add_argument("--window-days", type=int, default=90)
            p.add_argument(
                "--method",
                choices=(METHOD, "candidate-c2"),
                default=METHOD,
                help="the app's method, or candidate C2 (forecast-v1 + no High from one model "
                "without an ensemble; evaluated, not adopted)",
            )
            p.add_argument(
                "--scan-boundaries",
                action="store_true",
                help="TRAIN-only search for the single-source High / Moderate boundary",
            )


def run(args: argparse.Namespace) -> int:
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        if args.verify_cmd == "pair":
            from app.services.verification.pairing import DEFAULT_LOOKBACK, pair_forecasts

            lookback = (
                timedelta(days=args.lookback_days) if args.lookback_days else DEFAULT_LOOKBACK
            )
            summary = pair_forecasts(db, full=args.full, sources=args.source, lookback=lookback)
            print(json.dumps(summary.as_dict(), indent=2))
            return 0
        if args.verify_cmd == "metrics":
            from app.services.verification.jobs import WINDOWS_DAYS, compute_accuracy_metrics

            windows = (args.days,) if args.days else WINDOWS_DAYS
            print(json.dumps(compute_accuracy_metrics(db, windows_days=windows), indent=2))
            return 0
        if args.verify_cmd == "backtest":
            from app.services.verification.backtest import format_table, run_backtest

            try:
                result = run_backtest(
                    db,
                    source=args.source,
                    start=args.start,
                    end=args.end,
                    folds=args.folds,
                    station_codes=args.station,
                    n_boot=args.boot,
                    write=not args.no_write,
                    hs_kind=args.hs_kind,
                    lt_climatology_before=args.lt_climatology_before,
                    by_season=args.by_season,
                )
            except ValueError as exc:  # InsufficientDataError, or a refused option
                print(f"backtest: {exc}", file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps(result.as_dict(), indent=2, default=str))
            else:
                print(format_table(result))
                print(f"metrics rows written: {result.metrics_written}")
            return 0
        if args.verify_cmd in ("ranges", "confidence-classes"):
            return _validation(db, args)
    return 2


def _validation(db: Any, args: argparse.Namespace) -> int:
    from pathlib import Path

    from app.services.verification import validation
    from app.services.verification.ranges import format_coverage

    try:
        if args.verify_cmd == "ranges":
            result = validation.range_validation(
                db,
                station_codes=args.station,
                test_start=args.test_start,
                source=args.source,
                ensemble_source=args.ensemble_source,
                check_station_codes=args.check_station,
                start=args.start,
                n_boot=args.boot,
            )
            text = "\n".join(
                [
                    format_coverage(result["app_range"]["test"]["table"], "app range, TEST"),
                    f"decision: {result['app_range']['test']['decision']}",
                    format_coverage(result["fitted_range_test"]["table"], "fitted range, TEST"),
                    f"decision: {result['fitted_range_test']['decision']}",
                ]
            )
        else:
            result = validation.class_validation(
                db,
                station_codes=args.station,
                test_start=args.test_start,
                source=args.source,
                ensemble_source=args.ensemble_source,
                start=args.start,
                window_days=args.window_days,
                method=args.method,
                scan_boundaries=args.scan_boundaries,
                n_boot=args.boot,
            )
            text = validation.format_class_table(result)
    except ValueError as exc:  # InsufficientDataError, LeakageError is a RuntimeError
        print(f"verify {args.verify_cmd}: {exc}", file=sys.stderr)
        return 1
    print(text)
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2, default=str))
        print(f"written: {args.json}")
    return 0
