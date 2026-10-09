"""Management commands.

python -m app.cli init-db          # alembic upgrade head + seed reference data
python -m app.cli migrate          # alembic upgrade head
python -m app.cli seed             # (re)load airports and surf spots from data/surf_spots
python -m app.cli pipeline         # run jobs A→G once, synchronously
python -m app.cli simulate-swell SLUG [--days-ahead 7] [--hours 60]
python -m app.cli check-config     # list missing credentials for configured live providers
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal, utcnow
from app.core.logging import configure_logging


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(cfg, "head")


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
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
    args = parser.parse_args(argv)

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
    if args.cmd == "check-config":
        missing = get_settings().missing_live_credentials()
        print(json.dumps({"missing": missing}))
        return 1 if missing else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
