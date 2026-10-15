"""data/tide_stations.json against the spots, the constants file and CO-OPS's station list."""

from __future__ import annotations

import json
from pathlib import Path

from app.services.geo import haversine_km
from app.services.tides import catalog, coops
from app.services.tides.harmonic_model import load_constants

REPO = Path(__file__).resolve().parents[3]
SPOTS = {s["slug"]: s for s in json.loads((REPO / "data/surf_spots/spots.json").read_text())}
CATALOG = catalog.load_catalog(REPO / "data" / "tide_stations.json")
CONSTANTS = load_constants(REPO / "data" / "tides" / "constants_eot20.json")
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "coops"
US_SPOTS = {
    "pipeline",
    "sunset-beach",
    "waimea-bay",
    "jaws-peahi",
    "mavericks",
    "ocean-beach-sf",
    "trestles",
    "rincon",
}


def test_catalog_is_valid_and_complete() -> None:
    spots = {slug: (s["latitude"], s["longitude"]) for slug, s in SPOTS.items()}
    points = {slug: (c.latitude, c.longitude) for slug, c in CONSTANTS.items()}
    assert catalog.validate_catalog(CATALOG, spots, points) == []
    assert set(CATALOG["spots"]) == set(SPOTS) and len(SPOTS) == 50


def test_distances_match_the_spot_coordinates() -> None:
    stations = {s["code"]: s for s in CATALOG["stations"]}
    for slug, entry in CATALOG["spots"].items():
        spot = SPOTS[slug]
        for link in entry.get("stations", []):
            where = stations.get(link["code"], link)
            d = haversine_km(
                spot["latitude"], spot["longitude"], where["latitude"], where["longitude"]
            )
            assert abs(d - link["distance_km"]) <= 0.5, (slug, link["code"], d)
            assert link["justification"].strip()


def test_us_spots_use_coops_and_the_rest_the_model_or_say_unavailable() -> None:
    for slug, entry in CATALOG["spots"].items():
        if "unavailable" in entry:
            assert slug not in US_SPOTS and entry["unavailable"].strip()
            continue
        primary = next(lk for lk in entry["stations"] if lk["relation"] == "primary")
        if slug in US_SPOTS:
            assert primary["provider"] == "noaa_coops", slug
            fallback = [lk for lk in entry["stations"] if lk["relation"] == "fallback"]
            assert [lk["provider"] for lk in fallback] == ["harmonic_model"]
        else:
            assert primary["provider"] == "harmonic_model", slug
            assert slug in CONSTANTS, f"{slug}: model point without constants"
            assert primary["code"] == f"eot20:{slug}"


def test_stations_agree_with_the_coops_station_list() -> None:
    listed = coops.parse_station_list(
        json.loads((FIXTURES / "stations_tidepredictions.trimmed.json").read_text())
    )
    for s in CATALOG["stations"]:
        ref = listed[s["code"]]
        assert s["kind"] == ref.kind and s["reference_code"] == ref.reference_code
        assert haversine_km(s["latitude"], s["longitude"], ref.latitude, ref.longitude) < 0.01
        assert s["datum"] == "MLLW"


def test_validation_catches_a_wrong_distance_and_a_missing_reason() -> None:
    bad = json.loads(json.dumps(CATALOG))
    bad["spots"]["rincon"]["stations"][0]["distance_km"] = 40.0
    bad["spots"]["nazare"] = {"unavailable": " "}
    spots = {slug: (s["latitude"], s["longitude"]) for slug, s in SPOTS.items()}
    problems = catalog.validate_catalog(bad, spots)
    assert any(p.startswith("rincon/9411270: distance") for p in problems)
    assert "nazare: 'unavailable' needs a reason" in problems
