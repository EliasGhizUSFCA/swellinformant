"""Tide station catalog (``data/tide_stations.json``) and its seeding.

The catalog pairs every surf spot with the point(s) its tide is predicted at:

* a NOAA CO-OPS station for the US spots (``provider`` "noaa_coops", relation
  ``primary``), with the EOT20 model cell as ``fallback``;
* the EOT20 global tide model cell nearest the break elsewhere (``provider``
  "harmonic_model", code ``eot20:<slug>``, relation ``primary``), whose constants are in
  data/tides/constants_eot20.json;
* or ``{"unavailable": reason}`` when neither exists (the tide block then says so).

Each link carries ``distance_km`` (great-circle, break to station) and a justification; a
model link may carry a ``caveat`` (estuary, reef pass, strait, very small tide: places where
a global model is known to be weaker), shown with the tide. :func:`seed_tide_catalog` loads
it into ``tide_stations`` and ``spot_tide_stations``; it runs from ``python -m app.cli
tides seed`` and at the start of every ``tides.acquire`` job (idempotent).
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import SpotTideStation, SurfSpot, TideStation
from app.services.geo import haversine_km
from app.services.tides import coops
from app.services.tides.harmonic_model import ConstantsError, load_model

logger = logging.getLogger(__name__)

CATALOG_FILENAME = "tide_stations.json"
DEFAULT_CATALOG = Path(__file__).resolve().parents[4] / "data" / CATALOG_FILENAME
PROVIDERS = ("noaa_coops", "harmonic_model")
RELATIONS = ("primary", "fallback")
# Catalog distances are rounded to 0.1 km; anything further off is a wrong position.
DISTANCE_TOLERANCE_KM = 0.5
# Written by the acquisition job, never by the catalog: kept when re-seeding.
RUNTIME_KEYS = ("coops_checked_at", "last_acquired_at", "last_error", "last_error_at")


class CatalogError(ValueError):
    pass


def resolve_catalog_path(path: str | Path | None = None) -> Path:
    """Explicit path, else the repository data/ dir (dev) or /data (Docker image)."""
    candidates = [Path(path)] if path else []
    candidates += [
        DEFAULT_CATALOG,
        Path("/data") / CATALOG_FILENAME,
        Path.cwd() / "data" / CATALOG_FILENAME,
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        f"{CATALOG_FILENAME} not found in any of: {[str(c) for c in candidates]}"
    )


@lru_cache(maxsize=4)
def _load_cached(path: str, mtime: float) -> dict[str, Any]:
    return dict(json.loads(Path(path).read_text(encoding="utf-8")))


def load_catalog(path: str | Path | None = None) -> dict[str, Any]:
    resolved = resolve_catalog_path(path)
    return _load_cached(str(resolved), resolved.stat().st_mtime)


def validate_catalog(
    catalog: dict[str, Any],
    spots: dict[str, tuple[float, float]] | None = None,
    model_points: dict[str, tuple[float, float]] | None = None,
    *,
    complete: bool = True,
) -> list[str]:
    """Problems with the catalog.

    ``spots`` maps slug -> (lat, lon) of the break, ``model_points`` slug -> (lat, lon) of
    the constants file's cell; when given, distances and cell positions are checked, and
    with ``complete`` every spot must have an entry and every entry a known spot.
    """
    problems: list[str] = []
    stations = catalog.get("stations")
    if not isinstance(stations, list):
        return ["catalog has no 'stations' list"]
    by_code: dict[str, dict[str, Any]] = {}
    for s in stations:
        code = str(s.get("code", ""))
        if s.get("provider") != "noaa_coops":
            problems.append(f"{code}: only noaa_coops stations are listed under 'stations'")
        if not coops.STATION_ID_RE.fullmatch(code):
            problems.append(f"{code!r}: invalid CO-OPS station id")
        if code in by_code:
            problems.append(f"{code}: duplicate station")
        by_code[code] = s
        if s.get("kind") not in ("harmonic", "subordinate"):
            problems.append(f"{code}: kind must be harmonic or subordinate")
        if s.get("kind") == "subordinate" and not s.get("reference_code"):
            problems.append(f"{code}: a subordinate station needs its reference_code")
        if s.get("datum") != "MLLW":
            problems.append(f"{code}: CO-OPS predictions are stored relative to MLLW")
        if not s.get("position_source"):
            problems.append(f"{code}: position_source is required")
        try:
            lat, lon = float(s["latitude"]), float(s["longitude"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"{code}: latitude/longitude missing")
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            problems.append(f"{code}: position out of range")
    entries = catalog.get("spots")
    if not isinstance(entries, dict):
        return [*problems, "catalog has no 'spots' object"]
    if spots is not None and complete:
        missing = sorted(set(spots) - set(entries))
        if missing:
            problems.append(f"spots without a tide entry: {missing}")
    for slug, entry in entries.items():
        if spots is not None and slug not in spots:
            if complete:
                problems.append(f"{slug}: unknown spot")
            continue
        if "unavailable" in entry:
            if not str(entry["unavailable"]).strip():
                problems.append(f"{slug}: 'unavailable' needs a reason")
            continue
        links = entry.get("stations") or []
        if not links:
            problems.append(f"{slug}: no stations and no 'unavailable' reason")
        primaries = [lk for lk in links if lk.get("relation") == "primary"]
        if len(primaries) != 1:
            problems.append(f"{slug}: exactly one primary link expected")
        for link in links:
            provider, code = link.get("provider"), str(link.get("code", ""))
            if provider not in PROVIDERS:
                problems.append(f"{slug}: unknown provider {provider!r}")
                continue
            if link.get("relation") not in RELATIONS:
                problems.append(f"{slug}/{code}: unknown relation {link.get('relation')!r}")
            if not str(link.get("justification", "")).strip():
                problems.append(f"{slug}/{code}: no justification")
            if provider == "noaa_coops":
                station = by_code.get(code)
                if station is None:
                    problems.append(f"{slug}: station {code} is not in 'stations'")
                    continue
                position = (float(station["latitude"]), float(station["longitude"]))
            else:
                if code != f"eot20:{slug}":
                    problems.append(f"{slug}: model point code must be 'eot20:{slug}'")
                try:
                    position = (float(link["latitude"]), float(link["longitude"]))
                except (KeyError, TypeError, ValueError):
                    problems.append(f"{slug}/{code}: model point needs latitude/longitude")
                    continue
                if model_points is not None:
                    cell = model_points.get(slug)
                    if cell is None:
                        problems.append(f"{slug}: no constants for {code}")
                    elif max(abs(cell[0] - position[0]), abs(cell[1] - position[1])) > 1e-4:
                        problems.append(f"{slug}: {code} position differs from the constants")
            if spots is not None:
                d = haversine_km(*spots[slug], *position)
                if abs(d - float(link.get("distance_km", -1))) > DISTANCE_TOLERANCE_KM:
                    problems.append(
                        f"{slug}/{code}: distance is {d:.1f} km, not {link.get('distance_km')}"
                    )
    return problems


def _model_points(constants_path: str | Path | None) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        meta, constants = load_model(constants_path)
    except ConstantsError as exc:
        logger.warning("Tide constants unavailable: %s", exc)
        return {}, {}
    return meta, {slug: (c.latitude, c.longitude) for slug, c in constants.items()}


def seed_tide_catalog(
    db: Session, path: str | Path | None = None, constants_path: str | Path | None = None
) -> dict[str, Any]:
    """Upsert ``tide_stations`` and replace ``spot_tide_stations`` from the catalog.

    Idempotent; keys the acquisition job wrote into a station's metadata survive. Spots
    that are not in the database (not seeded yet) are skipped. The caller commits.
    """
    catalog = load_catalog(path)
    spot_rows = db.execute(
        select(SurfSpot.id, SurfSpot.slug, SurfSpot.name, SurfSpot.latitude, SurfSpot.longitude)
    ).all()
    spots = {row.slug: (row.latitude, row.longitude) for row in spot_rows}
    meta, points = _model_points(constants_path)
    # Spots missing from the database (or from the catalog) are skipped, not errors: the
    # spot seed and the tide catalog can be deployed in either order.
    problems = validate_catalog(catalog, spots, points if meta else None, complete=False)
    if problems:
        raise CatalogError("; ".join(problems))
    existing = {(s.provider, s.code): s for s in db.scalars(select(TideStation)).all()}
    created = updated = 0

    def upsert(provider: str, code: str, values: dict[str, Any]) -> TideStation:
        nonlocal created, updated
        station = existing.get((provider, code))
        runtime: dict[str, Any] = {}
        if station is None:
            station = TideStation(provider=provider, code=code)
            db.add(station)
            existing[(provider, code)] = station
            created += 1
        else:
            runtime = {k: v for k, v in (station.metadata_ or {}).items() if k in RUNTIME_KEYS}
            updated += 1
        for key, value in values.items():
            if key == "metadata_":
                station.metadata_ = {**value, **runtime}
            else:
                setattr(station, key, value)
        return station

    for s in catalog["stations"]:
        upsert(
            "noaa_coops",
            str(s["code"]),
            {
                "name": s["name"],
                "latitude": float(s["latitude"]),
                "longitude": float(s["longitude"]),
                "kind": s["kind"],
                "reference_code": s.get("reference_code"),
                "licence": coops.LICENCE,
                "attribution": coops.ATTRIBUTION,
                "metadata_": {
                    "state": s.get("state"),
                    "datum": s["datum"],
                    "position_source": s["position_source"],
                },
            },
        )
    names = {row.slug: row.name for row in spot_rows}
    links = 0
    for row in spot_rows:
        entry = catalog["spots"].get(row.slug)
        db.execute(delete(SpotTideStation).where(SpotTideStation.spot_id == row.id))
        if entry is None or "unavailable" in entry:
            continue
        for link in entry["stations"]:
            if link["provider"] == "harmonic_model":
                if row.slug not in points:
                    continue  # no constants file: the model point cannot be used
                station = upsert(
                    "harmonic_model",
                    link["code"],
                    {
                        "name": f"EOT20 model point near {names[row.slug]}",
                        "latitude": float(link["latitude"]),
                        "longitude": float(link["longitude"]),
                        "kind": "model",
                        "reference_code": None,
                        "datums": {},
                        "licence": str(meta.get("licence") or "CC BY 4.0"),
                        "attribution": str(meta.get("attribution") or ""),
                        "metadata_": {
                            "source": str(meta.get("source") or "eot20"),
                            "datum": "model_msl",
                            "caveat": link.get("caveat"),
                            "grid": meta.get("grid"),
                        },
                    },
                )
            else:
                station = existing[("noaa_coops", str(link["code"]))]
            db.flush()
            db.add(
                SpotTideStation(
                    spot_id=row.id,
                    station_id=station.id,
                    distance_km=float(link["distance_km"]),
                    relation=link["relation"],
                    justification=link["justification"],
                )
            )
            links += 1
    db.flush()
    logger.info(
        "Seeded %d tide stations (%d new), %d spot links", created + updated, created, links
    )
    return {"tide_stations": created + updated, "created": created, "spot_links": links}


def spot_links(db: Session, spot_id: int) -> list[tuple[SpotTideStation, TideStation]]:
    """The spot's tide stations, primary first."""
    rows = db.execute(
        select(SpotTideStation, TideStation)
        .join(TideStation, TideStation.id == SpotTideStation.station_id)
        .where(SpotTideStation.spot_id == spot_id)
    ).all()
    order = {"primary": 0, "fallback": 1}
    return sorted(
        ((link, station) for link, station in rows),
        key=lambda pair: (order.get(pair[0].relation or "", 2), pair[0].distance_km),
    )
