"""Observation station catalog (``data/observation_stations.json``) and its seeding.

The JSON file is the single source of truth for which buoys we use, where they are and
which surf spots they inform. Positions and depths in it were taken from each provider's
own data (``position_source`` says from which file); NDBC positions are the GFS-Wave
bulletin header positions for the same WMO id until an NDBC ingest replaces them from
``activestations.xml`` (www.ndbc.noaa.gov was unreachable when the catalog was built).

Each station row keeps everything except the core columns in ``buoy_stations.metadata``:
``provider_id``, provider-specific site names, ``bulletin_id`` (the GFS-Wave station
bulletin used for point-forecast archiving, only when within ``max_bulletin_distance_km``),
``paired_spots`` with the justification of each pairing, coverage and caveats. Keys the
ingest writes at run time (``last_observation_at``, ``last_reported_position``) survive
re-seeding.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import data_files
from app.models import BuoyStation, SurfSpot
from app.services.geo import haversine_km

logger = logging.getLogger(__name__)

CATALOG_FILENAME = "observation_stations.json"
DEFAULT_CATALOG = data_files.REPO_ROOT / "data" / CATALOG_FILENAME
PROVIDERS = ("aodn", "metoffice", "ndbc")
RELATIONS = (
    "offshore_reference",
    "nearshore_reference",
    "upstream_regional",
    "historical_reference",
)
CORE_FIELDS = ("code", "provider", "name", "latitude", "longitude", "water_depth_m")
# Written by the ingest, never by the catalog: kept when the catalog is re-seeded.
RUNTIME_KEYS = ("last_observation_at", "last_reported_position", "provider_position")


class CatalogError(ValueError):
    pass


def resolve_catalog_path(path: str | Path | None = None) -> Path:
    """Explicit path, else data/observation_stations.json where app.core.data_files finds it."""
    return data_files.find(CATALOG_FILENAME, path)


def load_catalog(path: str | Path | None = None) -> dict[str, Any]:
    return dict(json.loads(resolve_catalog_path(path).read_text()))


def validate_catalog(
    catalog: dict[str, Any], spots: dict[str, tuple[float, float]] | None = None
) -> list[str]:
    """Problems with the catalog; ``spots`` maps slug -> (lat, lon) when known."""
    problems: list[str] = []
    stations = catalog.get("stations")
    if not isinstance(stations, list) or not stations:
        return ["catalog has no 'stations' list"]
    max_bulletin_km = float(catalog.get("max_bulletin_distance_km", 30))
    codes = [s.get("code") for s in stations]
    if len(codes) != len(set(codes)):
        problems.append("duplicate station codes")
    for s in stations:
        code = s.get("code", "?")
        missing = [f for f in (*CORE_FIELDS, "provider_id", "licence", "attribution") if f not in s]
        if missing:
            problems.append(f"{code}: missing {missing}")
            continue
        if s["provider"] not in PROVIDERS:
            problems.append(f"{code}: unknown provider {s['provider']!r}")
        if not str(code).startswith(f"{s['provider']}:"):
            problems.append(f"{code}: code must start with '{s['provider']}:'")
        if not (-90 <= s["latitude"] <= 90 and -180 <= s["longitude"] <= 180):
            problems.append(f"{code}: position out of range")
        if not s.get("position_source"):
            problems.append(f"{code}: position_source is required (positions come from data)")
        if s.get("bulletin_id"):
            pos = s.get("bulletin_position") or {}
            try:
                d = haversine_km(s["latitude"], s["longitude"], pos["latitude"], pos["longitude"])
            except KeyError:
                problems.append(f"{code}: bulletin_position missing")
            else:
                if d > max_bulletin_km:
                    problems.append(f"{code}: bulletin {s['bulletin_id']} is {d:.0f} km away")
        for p in s.get("paired_spots", []):
            if p.get("relation") not in RELATIONS:
                problems.append(f"{code}: unknown relation {p.get('relation')!r}")
            if not p.get("note"):
                problems.append(f"{code}: pairing with {p.get('slug')} has no justification")
            if spots is None:
                continue
            if p.get("slug") not in spots:
                problems.append(f"{code}: unknown spot {p.get('slug')!r}")
                continue
            lat, lon = spots[p["slug"]]
            d = haversine_km(s["latitude"], s["longitude"], lat, lon)
            if abs(d - float(p.get("distance_km", -1))) > 0.5:
                problems.append(
                    f"{code}: distance to {p['slug']} is {d:.1f} km, not {p['distance_km']}"
                )
    return problems


def _metadata(entry: dict[str, Any]) -> dict[str, Any]:
    skip = {*CORE_FIELDS, "licence", "attribution", "is_active"}
    return {k: v for k, v in entry.items() if k not in skip}


def seed_stations(db: Session, path: str | Path | None = None) -> dict[str, Any]:
    """Upsert ``buoy_stations`` from the catalog (by code). Idempotent."""
    catalog = load_catalog(path)
    spot_rows = db.execute(select(SurfSpot.slug, SurfSpot.latitude, SurfSpot.longitude)).all()
    spots = {slug: (lat, lon) for slug, lat, lon in spot_rows} or None
    problems = validate_catalog(catalog, spots)
    if problems:
        raise CatalogError("; ".join(problems))
    created = updated = 0
    for entry in catalog["stations"]:
        station = db.scalar(select(BuoyStation).where(BuoyStation.code == entry["code"]))
        if station is None:
            station = BuoyStation(code=entry["code"])
            db.add(station)
            created += 1
            runtime: dict[str, Any] = {}
        else:
            updated += 1
            runtime = {k: v for k, v in (station.metadata_ or {}).items() if k in RUNTIME_KEYS}
        station.provider = entry["provider"]
        station.name = entry["name"]
        station.water_depth_m = entry.get("water_depth_m")
        station.is_active = bool(entry.get("is_active", True))
        station.license = entry["licence"]
        station.attribution = entry["attribution"]
        meta = _metadata(entry)
        station.latitude = entry["latitude"]
        station.longitude = entry["longitude"]
        provider_position = runtime.get("provider_position")
        if provider_position:
            # The provider's own station metadata (NDBC activestations.xml) replaced the
            # catalog position; keep it rather than reverting to the catalog value.
            station.latitude = provider_position["latitude"]
            station.longitude = provider_position["longitude"]
            station.name = provider_position.get("name") or station.name
            meta["position_source"] = provider_position["source"]
            meta["position_verified"] = True
        station.metadata_ = {**meta, **runtime}
    db.commit()
    logger.info("Seeded %d observation stations (%d new)", created + updated, created)
    return {"stations": created + updated, "created": created, "updated": updated}


def stations_for_provider(
    db: Session, provider: str, codes: list[str] | None = None
) -> list[BuoyStation]:
    """Active stations of a provider, or exactly the given codes (active or not)."""
    query = select(BuoyStation).where(BuoyStation.provider == provider)
    if codes:
        query = query.where(BuoyStation.code.in_(codes))
    else:
        query = query.where(BuoyStation.is_active)
    return list(db.scalars(query.order_by(BuoyStation.code)).all())


def bulletin_stations(db: Session, codes: list[str] | None = None) -> list[BuoyStation]:
    """Stations that have a GFS-Wave station bulletin within the pairing distance."""
    query = select(BuoyStation).where(BuoyStation.metadata_["bulletin_id"].astext.isnot(None))
    if codes:
        query = query.where(BuoyStation.code.in_(codes))
    else:
        query = query.where(BuoyStation.is_active)
    return [
        s
        for s in db.scalars(query.order_by(BuoyStation.code)).all()
        if s.metadata_.get("bulletin_id")
    ]
