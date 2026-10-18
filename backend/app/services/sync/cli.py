"""``python -m app.cli sync ...``: the synced data pipeline (contract F8).

  sync check [--json]                 print the SyncPlan (exit 0)
  sync run [--force] [--only spots,ensemble,map] [--if-due] [--trigger cli|github]
                                      run one sync and print the SyncResult: exit 0 for
                                      success, partial or skipped, 1 for failed, 3 when
                                      another sync holds the lease (SyncBusy)
  sync daemon [--once] [--no-init-db] the long-running sync daemon
  sync publish                        publish the stored data now
  sync status [--limit N]             the newest sync_runs rows

The commands are registered with their final options; the sync-core package implements
them. Until then each prints a "not implemented" JSON object and exits
:data:`EXIT_NOT_IMPLEMENTED`.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from app.services.sync.types import ComponentKind

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BUSY = 3
EXIT_NOT_IMPLEMENTED = 4
COMPONENT_KINDS: tuple[ComponentKind, ...] = ("spots", "ensemble", "map")


def component_kinds(value: str) -> list[ComponentKind]:
    """``--only``: comma-separated component kinds (spots, ensemble, map)."""
    kinds = [v.strip() for v in value.split(",") if v.strip()]
    unknown = sorted(set(kinds) - set(COMPONENT_KINDS))
    if not kinds or unknown:
        raise argparse.ArgumentTypeError(
            f"--only takes a comma-separated subset of {','.join(COMPONENT_KINDS)}"
        )
    return [k for k in COMPONENT_KINDS if k in kinds]


def register(subparsers: Any) -> None:
    sync = subparsers.add_parser("sync", help="synced data pipeline (check, run, daemon)")
    sub = sync.add_subparsers(dest="sync_cmd", required=True)
    check = sub.add_parser("check", help="print the sync plan (provider metadata only)")
    check.add_argument("--json", action="store_true", help="machine-readable output")
    run = sub.add_parser("run", help="run one coordinated sync")
    run.add_argument("--force", action="store_true", help="sync even if nothing is new")
    run.add_argument(
        "--only", type=component_kinds, default=None, help="components: spots,ensemble,map"
    )
    run.add_argument("--if-due", action="store_true", help="only when a sync is due")
    run.add_argument("--trigger", choices=("cli", "github"), default="cli")
    daemon = sub.add_parser("daemon", help="run the sync daemon until SIGTERM")
    daemon.add_argument("--once", action="store_true", help="one iteration, then exit")
    daemon.add_argument("--no-init-db", action="store_true", help="skip migrate and seed")
    sub.add_parser("publish", help="publish the stored data now")
    status = sub.add_parser("status", help="the newest sync runs")
    status.add_argument("--limit", type=int, default=10)


def run(args: argparse.Namespace) -> int:
    print(
        json.dumps(
            {
                "status": "not_implemented",
                "command": f"sync {args.sync_cmd}",
                "detail": "not implemented yet: the sync-core package provides this command",
            }
        )
    )
    return EXIT_NOT_IMPLEMENTED
