"""``python -m app.cli tides ...``: tide stations and predictions.

  tides seed [--catalog PATH]        load data/tide_stations.json into tide_stations and
                                     spot_tide_stations (also done by every tides.acquire)
  tides acquire                      run the daily tide job now (TIDE_PROVIDERS)
  tides list                         stations with their spots and stored predictions
  tides prune                        delete predictions older than TIDE_RETENTION_PAST_DAYS

``tides acquire`` runs synchronously in this process (no worker needed); it needs
api.tidesandcurrents.noaa.gov reachable for the US stations, the harmonic model needs no
network.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import func, select

from app.core.database import SessionLocal, utcnow


def register(subparsers: Any) -> None:
    tides = subparsers.add_parser("tides", help="tide stations and predictions")
    sub = tides.add_subparsers(dest="tides_cmd", required=True)
    seed = sub.add_parser("seed", help="load the tide station catalog")
    seed.add_argument("--catalog", default=None, help="path to tide_stations.json")
    sub.add_parser("acquire", help="fetch / compute tide predictions now")
    sub.add_parser("list", help="list tide stations")
    sub.add_parser("prune", help="delete old tide predictions")


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _list(db: Any) -> list[dict[str, Any]]:
    from app.models import SpotTideStation, SurfSpot, TidePrediction, TideStation

    stored = {
        sid: (n, lo, hi)
        for sid, n, lo, hi in db.execute(
            select(
                TidePrediction.station_id,
                func.count(),
                func.min(TidePrediction.valid_time),
                func.max(TidePrediction.valid_time),
            ).group_by(TidePrediction.station_id)
        ).all()
    }
    spots: dict[int, list[str]] = {}
    for sid, slug, relation in db.execute(
        select(SpotTideStation.station_id, SurfSpot.slug, SpotTideStation.relation).join(
            SurfSpot, SurfSpot.id == SpotTideStation.spot_id
        )
    ).all():
        spots.setdefault(sid, []).append(f"{slug} ({relation})")
    out = []
    for s in db.scalars(select(TideStation).order_by(TideStation.provider, TideStation.code)):
        n, lo, hi = stored.get(s.id, (0, None, None))
        out.append(
            {
                "provider": s.provider,
                "code": s.code,
                "name": s.name,
                "kind": s.kind,
                "spots": sorted(spots.get(s.id, [])),
                "predictions": n,
                "first": lo,
                "last": hi,
                "last_error": (s.metadata_ or {}).get("last_error"),
            }
        )
    return out


def run(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        if args.tides_cmd == "seed":
            from app.services.tides.catalog import seed_tide_catalog

            result = seed_tide_catalog(db, args.catalog)
            db.commit()
            _print(result)
            return 0
        if args.tides_cmd == "acquire":
            from app.services.tides.ingest import acquire_tides

            _print(acquire_tides(db))
            return 0
        if args.tides_cmd == "list":
            _print(_list(db))
            return 0
        if args.tides_cmd == "prune":
            from app.services.tides.maintenance import prune_tide_predictions

            deleted = prune_tide_predictions(db, utcnow())
            db.commit()
            _print({"tide_predictions_deleted": deleted})
            return 0
    return 2
