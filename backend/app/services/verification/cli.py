"""``python -m app.cli verify ...``: pairing, rolling metrics and backtests.

  verify pair [--full] [--source S] [--lookback-days N]
  verify metrics [--days N]
  verify backtest --source S [--start ISO --end ISO] [--folds K] [--station CODE]
                  [--hs-kind spectral_hm0|h_third] [--boot N] [--no-write] [--json]
                  [--lt-climatology-before ISO] [--by-season]

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
    return 2
