"""Idempotent seeding of reference data (airports, surf spots, spot airports).

Run with ``python -m app.cli seed``. Re-running updates rows in place (upsert by IATA
code / slug), so the curated JSON files are the single source of truth and new spots can
be added without schema changes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Airport, SpotAirport, SurfSpot
from app.models.enums import BreakType, SkillLevel, TidePreference
from app.services.geo import haversine_km, window_contains_window

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "surf_spots"

SPOT_FIELDS = (
    "name",
    "country",
    "country_code",
    "region",
    "region_group",
    "latitude",
    "longitude",
    "forecast_latitude",
    "forecast_longitude",
    "timezone",
    "break_type",
    "wave_direction",
    "is_big_wave",
    "swell_window_min",
    "swell_window_max",
    "optimal_swell_direction_min",
    "optimal_swell_direction_max",
    "offshore_wind_direction",
    "min_swell_period_s",
    "ideal_swell_period_s",
    "tide_preference",
    "wave_height_min_ft",
    "wave_height_max_ft",
    "height_factor",
    "skill_level",
    "best_months",
    "seasonality",
    "accessibility_notes",
    "hazards",
    "description",
)


class SeedValidationError(ValueError):
    pass


def resolve_data_dir(data_dir: str | Path | None = None) -> Path:
    candidates = [Path(data_dir)] if data_dir else []
    candidates += [DEFAULT_DATA_DIR, Path("/data/surf_spots"), Path.cwd() / "data" / "surf_spots"]
    for c in candidates:
        if (c / "spots.json").exists():
            return c
    raise FileNotFoundError(f"spots.json not found in any of: {[str(c) for c in candidates]}")


def validate_spot(raw: dict[str, Any], airports: set[str]) -> list[str]:
    problems: list[str] = []
    slug = raw.get("slug", "?")
    missing = [f for f in ("slug", *SPOT_FIELDS, "airports") if f not in raw]
    if missing:
        return [f"{slug}: missing fields {missing}"]
    try:
        ZoneInfo(raw["timezone"])
    except Exception:
        problems.append(f"{slug}: invalid timezone {raw['timezone']!r}")
    if not -90 <= raw["latitude"] <= 90 or not -180 <= raw["longitude"] <= 180:
        problems.append(f"{slug}: coordinates out of range")
    dist = haversine_km(
        raw["latitude"], raw["longitude"], raw["forecast_latitude"], raw["forecast_longitude"]
    )
    if dist > 40:
        problems.append(f"{slug}: forecast point {dist:.0f} km from break (max 40)")
    if raw["wave_height_min_ft"] >= raw["wave_height_max_ft"]:
        problems.append(f"{slug}: wave height range inverted")
    if raw["min_swell_period_s"] > raw["ideal_swell_period_s"]:
        problems.append(f"{slug}: min period > ideal period")
    for f in (
        "swell_window_min",
        "swell_window_max",
        "optimal_swell_direction_min",
        "optimal_swell_direction_max",
        "offshore_wind_direction",
    ):
        if not 0 <= raw[f] < 360:
            problems.append(f"{slug}: {f} out of [0, 360)")
    if not window_contains_window(
        raw["swell_window_min"],
        raw["swell_window_max"],
        raw["optimal_swell_direction_min"],
        raw["optimal_swell_direction_max"],
    ):
        problems.append(f"{slug}: optimal swell window is not inside the exposure window")
    try:
        BreakType(raw["break_type"])
        SkillLevel(raw["skill_level"])
        if raw["tide_preference"] is not None:
            TidePreference(raw["tide_preference"])
    except ValueError as exc:
        problems.append(f"{slug}: {exc}")
    primaries = [a for a in raw["airports"] if a.get("is_primary")]
    if len(primaries) != 1:
        problems.append(f"{slug}: needs exactly one primary airport")
    for a in raw["airports"]:
        if a["iata"] not in airports:
            problems.append(f"{slug}: airport {a['iata']} not in airports.json")
    return problems


def seed_reference_data(db: Session, data_dir: str | Path | None = None) -> dict[str, int]:
    directory = resolve_data_dir(data_dir)
    airports_raw: list[dict[str, Any]] = json.loads((directory / "airports.json").read_text())
    spots_raw: list[dict[str, Any]] = json.loads((directory / "spots.json").read_text())

    codes = [a["iata"] for a in airports_raw]
    if len(codes) != len(set(codes)):
        raise SeedValidationError("duplicate IATA codes in airports.json")
    slugs = [s["slug"] for s in spots_raw]
    if len(slugs) != len(set(slugs)):
        raise SeedValidationError("duplicate slugs in spots.json")
    problems = [p for s in spots_raw for p in validate_spot(s, set(codes))]
    for a in airports_raw:
        try:
            ZoneInfo(a["timezone"])
        except Exception:
            problems.append(f"airport {a['iata']}: invalid timezone {a['timezone']!r}")
    if problems:
        raise SeedValidationError("; ".join(problems))

    for a in airports_raw:
        row = {
            k: a[k]
            for k in (
                "iata",
                "name",
                "city",
                "country",
                "country_code",
                "latitude",
                "longitude",
                "timezone",
            )
        }
        stmt = insert(Airport).values(row)
        db.execute(stmt.on_conflict_do_update(index_elements=["iata"], set_=row))

    created = updated = 0
    for raw in spots_raw:
        spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == raw["slug"]))
        if spot is None:
            spot = SurfSpot(slug=raw["slug"])
            db.add(spot)
            created += 1
        else:
            updated += 1
        for f in SPOT_FIELDS:
            setattr(spot, f, raw[f])
        spot.is_active = raw.get("is_active", True)
        db.flush()
        wanted = {a["iata"]: a for a in raw["airports"]}
        for link in list(spot.airports):
            if link.airport_iata not in wanted:
                spot.airports.remove(link)
        db.flush()
        current = {link.airport_iata: link for link in spot.airports}
        # Clear primary flags first so the "one primary per spot" index never trips mid-update.
        for link in current.values():
            link.is_primary = False
        db.flush()
        for iata, a in wanted.items():
            found: SpotAirport | None = current.get(iata)
            if found is None:
                link = SpotAirport(spot_id=spot.id, airport_iata=iata)
                spot.airports.append(link)
            else:
                link = found
            link.is_primary = bool(a.get("is_primary"))
            link.transfer_minutes = int(a["transfer_minutes"])
            link.transfer_mode = a.get("transfer_mode", "car")
            link.notes = a.get("notes", "")
        db.flush()
    db.commit()
    logger.info("Seeded %d airports, %d new spots, %d updated", len(airports_raw), created, updated)
    return {"airports": len(airports_raw), "spots_created": created, "spots_updated": updated}
