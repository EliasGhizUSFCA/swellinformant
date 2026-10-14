"""Management commands.

python -m app.cli init-db          # alembic upgrade head + seed reference data
python -m app.cli migrate          # alembic upgrade head
python -m app.cli seed             # (re)load airports and surf spots from data/surf_spots
python -m app.cli pipeline         # run jobs A→G once, synchronously
python -m app.cli simulate-swell SLUG [--days-ahead 7] [--hours 60]
python -m app.cli check-config     # list missing credentials for configured live providers

Command groups provided by feature packages (``python -m app.cli GROUP --help``):
  obs     observation stations and buoy data      (app.services.observations.cli)
  verify  forecast verification and backtests     (app.services.verification.cli)
  ml      ML experiments and the model registry   (app.services.ml.cli)
  engine  ensemble, blending and spot profiles     (app.services.forecasts.cli)
  tides   tide stations and predictions           (app.services.tides.cli, once it exists)
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
from datetime import timedelta
from pathlib import Path
from types import ModuleType

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal, utcnow
from app.core.logging import configure_logging


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(cfg, "head")


# Each module exposes ``register(subparsers) -> None`` (adding one parser named after the
# group) and ``run(args) -> int``.
PLUGIN_GROUPS = {
    "obs": "app.services.observations.cli",
    "verify": "app.services.verification.cli",
    "ml": "app.services.ml.cli",
    "engine": "app.services.forecasts.cli",
    "research": "app.services.research.cli",
    # Tide stations and predictions (the module is added with the tide providers).
    "tides": "app.services.tides.cli",
}


def _plugins() -> dict[str, ModuleType]:
    found: dict[str, ModuleType] = {}
    for group, module in PLUGIN_GROUPS.items():
        try:
            spec = importlib.util.find_spec(module)
        except ModuleNotFoundError as exc:
            if exc.name and module.startswith(exc.name):
                continue  # the feature package is not installed
            raise
        if spec is not None:
            found[group] = importlib.import_module(module)
    return found


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    plugins = _plugins()
    for plugin in plugins.values():
        plugin.register(sub)
    sub.add_parser("migrate")
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

    if args.cmd in ("migrate", "init-db"):
        migrate()
        print("database migrated")
    if args.cmd in ("seed", "init-db"):
        from app.services.seed import seed_reference_data

        with SessionLocal() as db:
            print(json.dumps(seed_reference_data(db, args.data_dir)))
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
