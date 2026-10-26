"""``python -m app.cli sync ...``: the synced data pipeline.

  sync check [--json]                 print the SyncPlan (exit 0)
  sync run [--force] [--only spots,ensemble,map] [--if-due] [--trigger cli|github]
                                      run one sync and print the SyncResult: exit 0 for
                                      success, partial or skipped, 1 for failed, 3 when
                                      another sync holds the lease (SyncBusy)
  sync daemon [--once] [--no-init-db] the long-running sync daemon
  sync publish                        publish the stored data now
  sync status [--limit N]             the newest sync_runs rows

``sync check`` reads the known runs from Redis (the database only when Redis has none) and
the providers' metadata, so it does what the daemon's idle check does. ``sync run`` plans
with the database and acquires every component with a newer run than stored, without the
coalescing and retry waits (``--if-due`` follows the daemon's rules instead); ``--force``
runs every stage, and the acquisitions still skip runs already stored. docs/sync.md.

Heavy imports stay inside :func:`run`, and nothing here imports Celery or kombu, so the
group registers on every install.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from app.services.sync.types import ComponentKind

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BUSY = 3
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
    command = {
        "check": _check,
        "run": _run,
        "daemon": _daemon,
        "publish": _publish,
        "status": _status,
    }[args.sync_cmd]
    return command(args)


def _print(value: Any) -> None:
    from app.services.sync.types import jsonable

    print(json.dumps(jsonable(value), indent=2))


def _plan_text(plan: Any) -> str:
    """A plan for people: one line per component."""
    lines = [
        f"checked {plan.checked_at:%Y-%m-%d %H:%M:%S} UTC: "
        + ("sync DUE" if plan.due else "no sync due")
        + (" (forced)" if plan.forced else "")
    ]
    if plan.anchor_cycle is not None:
        lines.append(f"anchor cycle {plan.anchor_cycle:%Y-%m-%d %H}Z")
    if plan.waiting_until is not None:
        lines.append(f"waiting until {plan.waiting_until:%Y-%m-%d %H:%M} UTC")
    for c in plan.components:
        lines.append(
            f"  {c.kind:<8} {c.source_code:<18} {c.state:<11} "
            f"stored={c.stored_run_key or '-'} available={c.available_run_key or '-'}"
            + (f"  ({c.detail})" if c.detail else "")
        )
    lines += [f"  - {r}" for r in plan.reasons]
    return "\n".join(lines)


def _check(args: argparse.Namespace) -> int:
    from app.core.database import SessionLocal
    from app.services.sync import state
    from app.services.sync.detect import load_known, plan_sync

    known = state.read_known()
    if known is None:  # first check: read the stored keys once, then Redis serves them
        with SessionLocal() as db:
            known = load_known(db)
    plan = plan_sync(None, known=known)
    state.write_known(known)
    if args.json:
        _print(plan.as_dict())
    else:
        print(_plan_text(plan))
    return EXIT_OK


def _exit_code(status: str) -> int:
    if status == "busy":
        return EXIT_BUSY
    return EXIT_FAILED if status in ("failed", "abandoned") else EXIT_OK


def _run(args: argparse.Namespace) -> int:
    from app.core.database import SessionLocal
    from app.services.sync import state
    from app.services.sync.detect import empty_known, plan_sync, promote_pending
    from app.services.sync.orchestrator import remember, run_sync, tick
    from app.services.sync.types import SyncBusy

    if args.if_due:
        out = tick(trigger=args.trigger)
        _print(out)
        return _exit_code(str(out.get("status")))
    known = state.read_known() or empty_known()
    try:
        with SessionLocal() as db:
            plan = plan_sync(db, known=known, force=args.force)
        state.write_known(known)
        if any(c.state == "in_progress" for c in plan.components):
            raise SyncBusy()
        result = run_sync(
            trigger=args.trigger, force=args.force, only=args.only, plan=promote_pending(plan)
        )
    except SyncBusy as exc:
        _print({"status": "busy", "detail": str(exc)})
        return EXIT_BUSY
    if result.sync_run_id is not None:
        remember(known, result)
    _print(result.as_dict())
    return _exit_code(result.status)


def _daemon(args: argparse.Namespace) -> int:
    from app.services.sync.daemon import run_daemon

    return run_daemon(once=args.once, init_db=False if args.no_init_db else None)


def _publish(args: argparse.Namespace) -> int:
    from app.core.database import SessionLocal
    from app.services import public_cache
    from app.services.sync.publish import publication_summary, publish

    with SessionLocal() as db:
        row = publish(db, trigger="cli")
        public_cache.note_published(row.id)
        _print(publication_summary(row))
    return EXIT_OK


def _status(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.models.sync import SyncRun

    with SessionLocal() as db:
        rows = db.scalars(
            select(SyncRun).order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).limit(args.limit)
        ).all()
        _print(
            [
                {
                    "id": r.id,
                    "trigger": r.trigger,
                    "status": r.status,
                    "started_at": r.started_at,
                    "finished_at": r.finished_at,
                    "published_at": r.published_at,
                    "anchor_cycle": r.anchor_cycle,
                    "map_run_id": r.map_run_id,
                    "blend_run_id": r.blend_run_id,
                    "error": r.error_message,
                    "stages": [
                        {k: s.get(k) for k in ("name", "status", "duration_ms", "cpu_s")}
                        for s in r.stages or []
                    ],
                }
                for r in rows
            ]
        )
    return EXIT_OK
