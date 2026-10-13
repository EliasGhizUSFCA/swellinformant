"""The committed spot-profile data files: physical_profiles.json (measured bathymetry) and
its BlueTopo evidence. These read the REAL committed files (no network); the measured
numbers themselves come from scripts/bathymetry_profiles.py."""

from __future__ import annotations

import json
from typing import Any

from app.services.seed import DEFAULT_DATA_DIR, validate_profile

PROFILES = DEFAULT_DATA_DIR / "physical_profiles.json"
EVIDENCE = DEFAULT_DATA_DIR / "research_evidence.json"
SPOTS = DEFAULT_DATA_DIR / "spots.json"
BLUETOPO_BUCKET = "https://noaa-ocs-nationalbathymetry-pds.s3.amazonaws.com/BlueTopo/"
# Verbatim in every BlueTopo tile's ImageDescription tag and on the BlueTopo specifications
# page (checked when the evidence was written).
BLUETOPO_EXCERPT = (
    "This dataset is a multi-layered raster and part of NOAA Office of Coast Survey BlueTopo "
    "bathymetry...The vertical coordinate reference system for this product is NAVD88"
)


def _load(path: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_committed_physical_profiles_validate_and_carry_provenance() -> None:
    profiles = _load(PROFILES)
    slugs = {s["slug"] for s in _load(SPOTS)}
    assert set(profiles) == {"mavericks", "ocean-beach-sf", "trestles", "rincon"}
    for slug, entry in profiles.items():
        assert slug in slugs
        assert validate_profile(slug, entry) == []
        quality = entry["data_quality"]
        measured = [k for k in ("shore_normal_deg", "forecast_point_depth_m") if k in entry]
        assert measured, slug
        # Every value written is labelled measured, and only measured values are written.
        assert {k: quality[k] for k in measured} == {k: "measured" for k in measured}
        assert "controlling_depth_m" not in entry  # no break coordinate is a take-off zone
        prov = entry["provenance"]
        assert prov["vertical_datum"] == "NAVD88" and prov["tiles"]
        for tile in prov["tiles"]:
            assert tile["geotiff"].startswith(BLUETOPO_BUCKET)
            assert len(tile["geotiff_sha256"]) == 64
            assert tile["delivered"][:4].isdigit()
        assert "BlueTopo" in entry["notes"]


def test_measured_values_not_applied_are_kept_with_a_reason() -> None:
    profiles = _load(PROFILES)
    spots = {s["slug"]: s for s in _load(SPOTS)}
    for slug, entry in profiles.items():
        skipped = entry["provenance"]["not_applied"]
        if "shore_normal_deg" in entry:
            assert "shore_normal_deg" not in skipped
            # The applied normal keeps the documented optimal window reachable (< 90 deg).
            spot = spots[slug]
            for d in (spot["optimal_swell_direction_min"], spot["optimal_swell_direction_max"]):
                assert abs((d - entry["shore_normal_deg"] + 180) % 360 - 180) < 90
        else:
            assert skipped["shore_normal_deg"]["reason"]
            assert 0 <= skipped["shore_normal_deg"]["value"] < 360


def test_every_bluetopo_tile_is_a_read_survey_source_with_a_verbatim_excerpt() -> None:
    profiles = _load(PROFILES)
    evidence = _load(EVIDENCE)["spots"]
    for slug, entry in profiles.items():
        sources = {s["key"]: s for s in evidence[slug]["sources"]}
        for tile in entry["provenance"]["tiles"]:
            src = sources[f"bluetopo-{tile['tile'].lower()}"]
            assert src["source_type"] == "survey_data"
            assert src["retrieval"] == "fetched"
            assert src["publisher"] == "NOAA Office of Coast Survey"
            assert src["url"] == tile["geotiff"]
            assert src["published_year"] == int(tile["delivered"][:4])
            (claim,) = src["claims"]
            assert claim["variable"] == "bathymetry" and claim["value"] is None
            assert claim["excerpt"] == BLUETOPO_EXCERPT
        # No stray BlueTopo source without a tile in the provenance.
        tiles = {f"bluetopo-{t['tile'].lower()}" for t in entry["provenance"]["tiles"]}
        assert {k for k in sources if k.startswith("bluetopo-")} == tiles


def test_validate_profile_rejects_bad_values() -> None:
    assert validate_profile("x", {"shore_normal_deg": 200.0, "provenance": {"a": 1}}) == []
    problems = validate_profile(
        "x",
        {
            "provenance": "tile",
            "forecast_point_depth_m": -3,
            "controlling_depth_m": True,
            "shore_normal_deg": 360,
            "depth": 1,
        },
    )
    text = " ".join(problems)
    for expected in (
        "unknown profile fields ['depth']",
        "provenance must be an object",
        "forecast_point_depth_m must be a positive number",
        "controlling_depth_m must be a positive number",
        "shore_normal_deg must be in [0, 360)",
    ):
        assert expected in text, expected
