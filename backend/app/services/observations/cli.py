"""``python -m app.cli obs ...``: observation stations, buoy ingestion and point forecasts.

  obs seed-stations [--catalog PATH]            load data/observation_stations.json
  obs ingest [--provider P] [--since ISO --until ISO] [--station CODE ...]
             [--aodn-dataset delayed_qc|realtime]
  obs list                                      stations with observation / forecast counts
  obs collect-forecasts --start ISO --end ISO [--cycles 0,12] [--station CODE ...]
                        [--bulletin-id ID  (one --station; proxy bulletin, diagnostics only)]
                        [--source gfswave_bulletin|gefswave_station]

``--source gefswave_station`` archives the GEFS-Wave ensemble mean and spread at the
stations (verification.gefs_bulletins; cycles from 2026-02-26 only, the first with spreads).

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
    ingest.add_argument(
        "--aodn-dataset",
        action="append",
        choices=("delayed_qc", "realtime"),
        default=None,
        help="AODN only: read just this dataset (repeatable; default both). Use delayed_qc "
        "for a backfill that must contain quality-controlled data only",
    )
    sub.add_parser("list", help="list stations")
    collect = sub.add_parser("collect-forecasts", help="archive GFS-Wave station bulletins")
    collect.add_argument("--start", type=_iso, required=True)
    collect.add_argument("--end", type=_iso, required=True)
    collect.add_argument("--cycles", type=_cycles, default=(0, 12))
    collect.add_argument("--station", action="append", default=None, help="station code")
    collect.add_argument(
        "--bulletin-id",
        default=None,
        help="archive this bulletin for the one --station instead of its catalog bulletin; "
        "stored as source gfswave_bulletin_proxy (diagnostics only) unless it is the catalog id",
    )
    collect.add_argument(
        "--source",
        choices=("gfswave_bulletin", "gefswave_station"),
        default="gfswave_bulletin",
        help="gfswave_bulletin (deterministic GFS-Wave, default) or gefswave_station "
        "(GEFS-Wave ensemble mean and spread)",
    )


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
            if args.source == "gefswave_station":
                from app.services.verification import gefs_bulletins

                if args.bulletin_id:
                    print("--bulletin-id applies to gfswave_bulletin only", file=sys.stderr)
                    return 2
                _print(
                    gefs_bulletins.collect_range(
                        db, args.start, args.end, args.cycles, args.station
                    )
                )
                return 0
            try:
                out = collect_range(
                    db,
                    args.start,
                    args.end,
                    args.cycles,
                    args.station,
                    bulletin_override=args.bulletin_id,
                )
            except ValueError as exc:
                print(f"obs collect-forecasts: {exc}", file=sys.stderr)
                return 2
            _print(out)
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
    datasets = None
    if args.aodn_dataset:
        if providers != ["aodn"]:
            print("--aodn-dataset needs --provider aodn", file=sys.stderr)
            return 2
        from app.services.observations.aodn import DELAYED, REALTIME

        names = {"delayed_qc": DELAYED, "realtime": REALTIME}
        datasets = [names[d] for d in dict.fromkeys(args.aodn_dataset)]
    out: dict[str, Any] = {}
    failures = 0
    for name in providers:
        try:
            out[name] = ingest_range(db, name, since, until, args.station, datasets=datasets)
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
