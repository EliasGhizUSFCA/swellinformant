"""``python -m app.cli engine ...``: forecast-engine commands (see app.cli.PLUGIN_GROUPS).

python -m app.cli engine ensemble [--force] [--members]   # GEFS-Wave mean/spread, prints JSON

To add a subcommand, write ``_add_<name>(subparsers)`` that adds its parser and sets
``handler`` with ``set_defaults``, then append the function to SUBCOMMANDS.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any

GROUP = "engine"

SubParsers = Any  # argparse's _SubParsersAction is private API


def _run_ensemble(args: argparse.Namespace) -> int:
    from app.core.database import SessionLocal
    from app.services.forecasts.ensemble import acquire_ensemble

    with SessionLocal() as db:
        result = acquire_ensemble(db, force=args.force, members=args.members)
    print(json.dumps(result, indent=2, default=str))
    return 1 if result.get("status") == "failed" else 0


def _add_ensemble(sub: SubParsers) -> None:
    p = sub.add_parser(
        "ensemble",
        help="fetch GEFS-Wave ensemble mean/spread for the newest complete cycle",
        description=(
            "Samples GEFS-Wave mean and spread (Hs, Tp, wind, swell partition 1) at every "
            "active spot into forecast_ensemble_stats. Without --force it does nothing "
            "unless GEFS_ENABLED=true, and skips a cycle that is already stored."
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="run even if GEFS_ENABLED=false and re-fetch an already stored cycle",
    )
    p.add_argument(
        "--members",
        action="store_true",
        help="also read HTSGW from the 31 member files for p10/p50/p90 (~12 MB more per step)",
    )
    p.set_defaults(handler=_run_ensemble)


SUBCOMMANDS: list[Callable[[SubParsers], None]] = [_add_ensemble]


def register(subparsers: SubParsers) -> None:
    parser = subparsers.add_parser(GROUP, help="ensemble, blending and spot profiles")
    nested = parser.add_subparsers(dest="engine_cmd", required=True)
    for add in SUBCOMMANDS:
        add(nested)


def run(args: argparse.Namespace) -> int:
    handler: Callable[[argparse.Namespace], int] = args.handler
    return int(handler(args))
