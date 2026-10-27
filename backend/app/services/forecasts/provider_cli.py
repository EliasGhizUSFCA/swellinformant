"""``python -m app.cli forecast ...``: the weather provider commands (see app.cli.PLUGIN_GROUPS).

python -m app.cli forecast provider-check [--provider open_meteo|windy]
    # prints the provider selection and its reasons, Windy's eligibility, the recorded
    # provider health and a live health check (Open-Meteo: one meta.json; Windy: one
    # sentinel request) as JSON; stores no forecast data and records no health.
    # Exit 0 when every checked provider is healthy, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any

GROUP = "forecast"
CHECKABLE = ("open_meteo", "windy")

SubParsers = Any  # argparse's _SubParsersAction is private API


def _run_provider_check(args: argparse.Namespace) -> int:
    from app.core.config import get_settings
    from app.services.forecasts.provider_manager import provider_check_report

    providers = [args.provider] if args.provider else None
    report = provider_check_report(get_settings(), providers)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["ok"] else 1


def _add_provider_check(sub: SubParsers) -> None:
    p = sub.add_parser(
        "provider-check",
        help="show the active weather provider, why, and a live health check",
        description=(
            "Prints the effective provider selection (FORECAST_PROVIDERS, else "
            "WEATHER_PROVIDER) and its reasons, Windy's eligibility (WINDY_API_KEY and "
            "WINDY_STORAGE_AUTHORIZED), the provider health recorded in Redis (a Windy "
            "fallback to Open-Meteo and until when) and a live health check: Open-Meteo reads "
            "one meta.json, Windy sends one sentinel request (it detects a testing key, an "
            "invalid key and a used-up quota, and counts against WINDY_DAILY_REQUEST_LIMIT). "
            "Without --provider it checks the configured primary and its fallback (Windy too "
            "while Open-Meteo stands in). Stores no forecast data and records no health; "
            "never prints a key."
        ),
    )
    p.add_argument("--provider", choices=CHECKABLE, help="check only this provider")
    p.set_defaults(handler=_run_provider_check)


SUBCOMMANDS: list[Callable[[SubParsers], None]] = [_add_provider_check]


def register(subparsers: SubParsers) -> None:
    parser = subparsers.add_parser(GROUP, help="weather provider selection and health checks")
    nested = parser.add_subparsers(dest="forecast_cmd", required=True)
    for add in SUBCOMMANDS:
        add(nested)


def run(args: argparse.Namespace) -> int:
    handler: Callable[[argparse.Namespace], int] = args.handler
    return int(handler(args))
