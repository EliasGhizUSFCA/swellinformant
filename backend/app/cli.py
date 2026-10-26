"""Management commands.

python -m app.cli init-db          # alembic upgrade head + seed reference data + tide catalog
python -m app.cli migrate          # alembic upgrade head
python -m app.cli migrate --on-build   # the same, only where MIGRATE_ON_BUILD and VERCEL_ENV
                                       # allow it (a build step); prints "skipped" otherwise
python -m app.cli seed             # (re)load airports and surf spots from data/surf_spots, then
                                   # the tide station catalog (data/tide_stations.json)
python -m app.cli pipeline         # run jobs A→G once, synchronously
python -m app.cli simulate-swell SLUG [--days-ahead 7] [--hours 60]
python -m app.cli check-config     # list missing credentials for configured live providers
python -m app.cli make-admin EMAIL # grant admin (developer tools) to an account

Command groups provided by feature packages (``python -m app.cli GROUP --help``):
  obs       observation stations and buoy data        (app.services.observations.cli)
  verify    forecast verification and backtests       (app.services.verification.cli)
  ml        ML experiments and the model registry     (app.services.ml.cli)
  engine    ensemble, map fields, Open-Meteo budget   (app.services.forecasts.cli)
  research  research evidence and research confidence (app.services.research.cli)
  tides     tide stations and predictions             (app.services.tides.cli)
  sync      synced data pipeline: check, run, daemon  (app.services.sync.cli)
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal, utcnow
from app.core.logging import configure_logging


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(cfg, "head")


def seed_database(data_dir: str | None = None) -> dict[str, Any]:
    """Reference data (airports, surf spots) and then the tide station catalog."""
    from app.services.seed import seed_reference_data
    from app.services.tides.catalog import seed_tide_catalog

    with SessionLocal() as db:
        seeded = seed_reference_data(db, data_dir)
        # After the spots: the catalog links stations and model points to seeded spots.
        # Here, not in seed_reference_data: the test session seeds reference data once and
        # truncates the tide tables after each test (docs/tides.md).
        tides = seed_tide_catalog(db)
        db.commit()
    return {"seed": seeded, "tide_catalog": tides}


def init_database(data_dir: str | None = None) -> dict[str, Any]:
    """``init-db``: migrate, seed and load the tide catalog (idempotent; the sync daemon
    runs it at start with SYNC_DAEMON_INIT_DB)."""
    migrate()
    return {"migrated": True, **seed_database(data_dir)}


def migrate_on_build() -> tuple[int, dict[str, Any]]:
    """``migrate --on-build``: migrate when MIGRATE_ON_BUILD allows it for this build.

    off: never. production: only when VERCEL_ENV=production (preview builds and Neon
    preview branches are left alone). all: every build. Migrations use the direct
    connection (migration_database_url, set by alembic/env.py). Returns (exit code, report).
    """
    import os

    from app.core.logging import redact

    setting = get_settings().migrate_on_build
    vercel_env = os.environ.get("VERCEL_ENV") or None
    report: dict[str, Any] = {
        "command": "migrate --on-build",
        "migrate_on_build": setting,
        "vercel_env": vercel_env,
    }
    if setting == "off":
        return 0, {**report, "status": "skipped", "reason": "MIGRATE_ON_BUILD=off"}
    if setting == "production" and vercel_env != "production":
        reason = (
            f"VERCEL_ENV={vercel_env or 'unset'}: MIGRATE_ON_BUILD=production migrates "
            "production builds only"
        )
        return 0, {**report, "status": "skipped", "reason": reason}
    try:
        migrate()
    except Exception as exc:  # the build fails rather than deploying against an old schema
        error = redact(f"{type(exc).__name__}: {exc}")[:2000]
        return 1, {**report, "status": "failed", "error": error}
    return 0, {**report, "status": "migrated"}


# Each module exposes ``register(subparsers) -> None`` (adding one parser named after the
# group) and ``run(args) -> int``.
PLUGIN_GROUPS = {
    "obs": "app.services.observations.cli",
    "verify": "app.services.verification.cli",
    "ml": "app.services.ml.cli",
    "engine": "app.services.forecasts.cli",
    "research": "app.services.research.cli",
    # Tide stations and predictions.
    "tides": "app.services.tides.cli",
    # The synced data pipeline: sync check / run / daemon / publish / status.
    "sync": "app.services.sync.cli",
}
# Top-level packages of the optional extras (worker: Celery; sync: the data libraries). A
# command group that needs one of them is left out of an install without that extra (the
# lean API install of a hosted deployment), so migrate, init-db and check-config still run
# there; with every extra installed (classic docker compose, development) nothing changes.
OPTIONAL_DEPENDENCIES = frozenset(
    {"celery", "kombu", "pandas", "eccodes", "gribapi", "pyarrow", "sklearn", "scipy", "joblib"}
)


def _missing_optional(exc: ModuleNotFoundError) -> bool:
    """True when ``exc`` is an optional dependency (an extra) that is not installed."""
    return (exc.name or "").split(".")[0] in OPTIONAL_DEPENDENCIES


def _plugins() -> dict[str, ModuleType]:
    found: dict[str, ModuleType] = {}
    for group, module in PLUGIN_GROUPS.items():
        try:
            found[group] = importlib.import_module(module)
        except ModuleNotFoundError as exc:
            if not _missing_optional(exc):
                raise
            print(f"note: '{group}' commands need the {exc.name!r} package", file=sys.stderr)
    return found


def _register(plugins: dict[str, ModuleType], sub: Any) -> dict[str, str]:
    """Add every group's parser. A group whose ``register`` needs a missing optional
    dependency is dropped from ``plugins``; returns {group: missing package}."""
    dropped: dict[str, str] = {}
    for group, plugin in list(plugins.items()):
        try:
            plugin.register(sub)
        except ModuleNotFoundError as exc:
            if not _missing_optional(exc):
                raise
            del plugins[group]
            dropped[group] = exc.name or "?"
            print(f"note: '{group}' commands need the {exc.name!r} package", file=sys.stderr)
    return dropped


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    plugins = _plugins()
    dropped = _register(plugins, sub)
    migrate_p = sub.add_parser("migrate")
    migrate_p.add_argument(
        "--on-build",
        action="store_true",
        help="migrate only where MIGRATE_ON_BUILD and VERCEL_ENV allow it (build step)",
    )
    seed_p = sub.add_parser("seed")
    seed_p.add_argument("--data-dir", default=None)
    init_p = sub.add_parser("init-db")
    init_p.add_argument("--data-dir", default=None)
    pipe = sub.add_parser("pipeline")
    pipe.add_argument("--force-acquire", action="store_true")
    sim = sub.add_parser("simulate-swell")
    sim.add_argument("slug")
    sim.add_argument("--days-ahead", type=float, default=7.0)
    sim.add_argument("--hours", type=int, default=60)
    sub.add_parser("check-config")
    admin = sub.add_parser("make-admin", help="grant admin (developer tools) to an account")
    admin.add_argument("email")
    args = parser.parse_args(argv)
    if args.cmd in plugins:
        return int(plugins[args.cmd].run(args))
    if args.cmd in dropped:
        print(f"'{args.cmd}' needs the {dropped[args.cmd]!r} package", file=sys.stderr)
        return 1
    if args.cmd == "migrate" and args.on_build:
        code, report = migrate_on_build()
        print(json.dumps(report))
        return code

    if args.cmd in ("migrate", "init-db"):
        migrate()
        print("database migrated")
    if args.cmd in ("seed", "init-db"):
        seeded = seed_database(args.data_dir)
        print(json.dumps(seeded["seed"]))
        print(json.dumps({"tide_catalog": seeded["tide_catalog"]}, default=str))
    if args.cmd == "pipeline":
        from app.services.pipeline import run_pipeline

        with SessionLocal() as db:
            print(
                json.dumps(
                    run_pipeline(db, force_acquire=args.force_acquire), indent=2, default=str
                )
            )
    if args.cmd == "simulate-swell":
        from app.models import SurfSpot
        from app.services.forecasts.ingestion import simulate_swell

        if "demo" not in get_settings().forecast_provider_list:
            print("simulate-swell only works when FORECAST_PROVIDERS=demo", file=sys.stderr)
            return 2
        with SessionLocal() as db:
            spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == args.slug))
            if spot is None:
                print(f"unknown spot {args.slug}", file=sys.stderr)
                return 1
            start = utcnow() + timedelta(days=args.days_ahead)
            print(
                json.dumps(
                    simulate_swell(db, spot, start=start, duration_hours=args.hours).as_dict()
                )
            )
    if args.cmd == "make-admin":
        from app.models import User
        from app.services.accounts import normalize_email

        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == normalize_email(args.email)))
            if user is None:
                print(f"no account for {args.email}", file=sys.stderr)
                return 1
            user.is_admin = True
            db.commit()
            print(json.dumps({"admin": True, "user_id": str(user.id)}))
    if args.cmd == "check-config":
        missing = get_settings().missing_live_credentials()
        print(json.dumps({"missing": missing}))
        return 1 if missing else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
