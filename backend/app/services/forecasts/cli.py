"""``python -m app.cli engine ...``: forecast-engine commands (see app.cli.PLUGIN_GROUPS).

python -m app.cli engine ensemble [--force] [--members]   # GEFS-Wave mean/spread, prints JSON
python -m app.cli engine map-fields [--cycle YYYYMMDDHH] [--max-lead-hours N] [--force]
    # global map fields (ocean layers) of the newest complete GFS cycle, prints JSON
python -m app.cli engine budget   # Open-Meteo weighted calls used per window, cache entries

To add a subcommand, write ``_add_<name>(subparsers)`` that adds its parser and sets
``handler`` with ``set_defaults``, then append the function to SUBCOMMANDS.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import UTC, datetime
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


def _cycle(text: str) -> datetime:
    try:
        return datetime.strptime(text, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYYMMDDHH, got {text!r}") from exc


def _lead_hours(text: str) -> int:
    value = int(text)
    if not 0 <= value <= 384:
        raise argparse.ArgumentTypeError("--max-lead-hours must be between 0 and 384")
    return value


def _run_map_fields(args: argparse.Namespace) -> int:
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from app.services.forecasts.ingestion import acquire_map_fields
    from app.services.map_fields import map_fields_mode

    mode = map_fields_mode(get_settings())
    if mode == "off":
        print(
            json.dumps(
                {
                    "status": "off",
                    "message": "map fields are off: set MAP_FIELDS_ENABLED=true, or leave it "
                    "unset and add noaa_gfs to FORECAST_PROVIDERS",
                }
            )
        )
        return 1
    with SessionLocal() as db:
        result = acquire_map_fields(
            db, cycle=args.cycle, max_lead_hours=args.max_lead_hours, force=args.force
        )
    print(json.dumps({"mode": mode, **result}, indent=2, default=str))
    return 1 if result.get("status") == "failed" else 0


def _add_map_fields(sub: SubParsers) -> None:
    p = sub.add_parser(
        "map-fields",
        help="fetch the global map fields (ocean layers) of the newest complete GFS cycle",
        description=(
            "Reads HTSGW, PERPW and DIRPW (GFS-Wave global.0p25) and 10 m wind and MSL "
            "pressure (GFS pgrb2.0p25) for every map hour, regrids them to "
            "MAP_FIELD_RESOLUTION_DEG and stores them under the source noaa_gfs_fields. "
            "Works in every map-fields mode except off; skips a cycle whose fields are "
            "already stored. Prints counts, bytes and the duration as JSON."
        ),
    )
    p.add_argument("--cycle", type=_cycle, help="GFS cycle YYYYMMDDHH (default: newest complete)")
    p.add_argument(
        "--max-lead-hours",
        type=_lead_hours,
        help="last forecast hour (default MAP_FIELD_MAX_LEAD_HOURS)",
    )
    p.add_argument("--force", action="store_true", help="re-fetch fields that are already stored")
    p.set_defaults(handler=_run_map_fields)


def _run_budget(args: argparse.Namespace) -> int:
    from app.services.forecasts import provider_cache
    from app.services.forecasts.quota import open_meteo_budget_usage

    usage = open_meteo_budget_usage()
    print(
        json.dumps(
            {
                "open_meteo_weighted_calls": usage or "unavailable (Redis not reachable)",
                "provider_cache_entries": provider_cache.cache_size(),
            },
            indent=2,
        )
    )
    return 0


def _add_budget(sub: SubParsers) -> None:
    p = sub.add_parser(
        "budget",
        help="Open-Meteo weighted calls used in the current minute, hour, day and month",
        description=(
            "Reads the call-budget counters in Redis (OPEN_METEO_CALLS_PER_MINUTE/HOUR/DAY/"
            "MONTH; both Open-Meteo families share them) and the number of cached provider "
            "responses. Read-only; metadata requests are free and not counted."
        ),
    )
    p.set_defaults(handler=_run_budget)


SUBCOMMANDS: list[Callable[[SubParsers], None]] = [_add_ensemble, _add_map_fields, _add_budget]


def register(subparsers: SubParsers) -> None:
    parser = subparsers.add_parser(GROUP, help="ensemble, map fields, blending and spot profiles")
    nested = parser.add_subparsers(dest="engine_cmd", required=True)
    for add in SUBCOMMANDS:
        add(nested)


def run(args: argparse.Namespace) -> int:
    handler: Callable[[argparse.Namespace], int] = args.handler
    return int(handler(args))
