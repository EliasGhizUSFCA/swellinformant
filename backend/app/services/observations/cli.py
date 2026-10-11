"""``python -m app.cli obs ...``: observation stations, buoy ingestion and point forecasts.

  obs seed-stations [--catalog PATH]            load data/observation_stations.json
  obs ingest [--provider P] [--since ISO --until ISO] [--station CODE ...]
  obs list                                      stations with observation / forecast counts
  obs collect-forecasts --start ISO --end ISO [--cycles 0,12] [--station CODE ...]

``obs ingest`` runs regardless of OBSERVATIONS_ENABLED (an operator asked for it); without
``--since`` it ingests the last OBSERVATION_LOOKBACK_HOURS. Times without a zone are UTC.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.database import SessionLocal, utcnow


def _iso(value: str) -> datetime:
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO date/time: {value!r}") from exc
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def _cycles(value: str) -> tuple[int, ...]:
    try:
        hours = tuple(int(x) for x in value.split(",") if x.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("cycles must be like 0,12") from exc
    if not hours or any(h not in (0, 6, 12, 18) for h in hours):
        raise argparse.ArgumentTypeError("cycles must be among 0, 6, 12, 18")
    return hours


def register(subparsers: Any) -> None:
    obs = subparsers.add_parser("obs", help="observation stations and buoy data")
    sub = obs.add_subparsers(dest="obs_cmd", required=True)
    seed = sub.add_parser("seed-stations", help="load the station catalog")
    seed.add_argument("--catalog", default=None, help="path to observation_stations.json")
    ingest = sub.add_parser("ingest", help="ingest buoy observations")
    ingest.add_argument("--provider", choices=("aodn", "metoffice", "ndbc"), default=None)
    ingest.add_argument("--since", type=_iso, default=None)
    ingest.add_argument("--until", type=_iso, default=None)
    ingest.add_argument("--station", action="append", default=None, help="station code")
    sub.add_parser("list", help="list stations")
    collect = sub.add_parser("collect-forecasts", help="archive GFS-Wave station bulletins")
    collect.add_argument("--start", type=_iso, required=True)
    collect.add_argument("--end", type=_iso, required=True)
    collect.add_argument("--cycles", type=_cycles, default=(0, 12))
    collect.add_argument("--station", action="append", default=None, help="station code")


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def run(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        if args.obs_cmd == "seed-stations":
            from app.services.observations.stations import seed_stations

            _print(seed_stations(db, args.catalog))
            return 0
        if args.obs_cmd == "ingest":
            return _ingest(db, args)
        if args.obs_cmd == "list":
            _print(_list(db))
            return 0
        if args.obs_cmd == "collect-forecasts":
            from app.services.verification.point_forecasts import collect_range

            if args.start >= args.end:
                print("--start must be before --end", file=sys.stderr)
                return 2
            _print(collect_range(db, args.start, args.end, args.cycles, args.station))
            return 0
    return 2


def _ingest(db: Any, args: argparse.Namespace) -> int:
    from app.services.http import ProviderError
    from app.services.observations.ingest import ingest_range

    settings = get_settings()
    until = args.until or utcnow()
    since = args.since or until - timedelta(hours=settings.observation_lookback_hours)
    if since >= until:
        print("--since must be before --until", file=sys.stderr)
        return 2
    providers = [args.provider] if args.provider else settings.observation_provider_list
    out: dict[str, Any] = {}
    failures = 0
    for name in providers:
        try:
            out[name] = ingest_range(db, name, since, until, args.station)
        except (ProviderError, ValueError) as exc:
            db.rollback()
            failures += 1
            out[name] = {"failed": str(exc), "transient": getattr(exc, "transient", False)}
    _print(out)
    return 1 if failures == len(providers) else 0


def _list(db: Any) -> list[dict[str, Any]]:
    from app.models import BuoyObservation, BuoyStation, PointForecast

    obs = {
        sid: (n, last)
        for sid, n, last in db.execute(
            select(
                BuoyObservation.station_id,
                func.count(),
                func.max(BuoyObservation.observed_at),
            )
            .where(BuoyObservation.qc_flag != "bad")
            .group_by(BuoyObservation.station_id)
        ).all()
    }
    fc = {
        sid: (n, last)
        for sid, n, last in db.execute(
            select(
                PointForecast.station_id, func.count(), func.max(PointForecast.cycle_time)
            ).group_by(PointForecast.station_id)
        ).all()
    }
    rows = []
    for s in db.scalars(select(BuoyStation).order_by(BuoyStation.code)).all():
        n_obs, last_obs = obs.get(s.id, (0, None))
        n_fc, last_cycle = fc.get(s.id, (0, None))
        rows.append(
            {
                "code": s.code,
                "active": s.is_active,
                "name": s.name,
                "lat": s.latitude,
                "lon": s.longitude,
                "depth_m": s.water_depth_m,
                "bulletin_id": (s.metadata_ or {}).get("bulletin_id"),
                "observations": n_obs,
                "latest_observation": last_obs,
                "point_forecasts": n_fc,
                "latest_cycle": last_cycle,
                "paired_spots": [p["slug"] for p in (s.metadata_ or {}).get("paired_spots", [])],
            }
        )
    return rows
