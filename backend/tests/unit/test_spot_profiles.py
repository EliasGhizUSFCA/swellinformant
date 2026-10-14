"""The committed spot-profile data files: physical_profiles.json (measured bathymetry and
researched wind profiles) and their evidence. These read the REAL committed files (no
network); the measured numbers themselves come from scripts/bathymetry_profiles.py, the wind
profiles from directly read claims in research_evidence.json."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.services.seed import (
    DEFAULT_DATA_DIR,
    SeedValidationError,
    evidence_retrievals,
    seed_physical_profiles,
    validate_profile,
)
from app.services.surf_quality.wind import cited_keys, wind_profile_from_dict

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


def _bathymetry() -> dict[str, Any]:
    """The entries measured from BlueTopo (the others carry a wind profile only)."""
    return {k: v for k, v in _load(PROFILES).items() if "provenance" in v}


def test_committed_physical_profiles_validate_and_carry_provenance() -> None:
    profiles = _bathymetry()
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
    profiles = _bathymetry()
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


def test_forecast_point_depths_come_from_survey_cells_only() -> None:
    """A depth from "NBS Generalization" cells (BlueTopo's best-guess surface outside any
    survey, uncertainty unknown) is not a measurement: it is never written as a "measured"
    forecast-point depth, only kept under not_applied (Trestles' forecast point)."""
    profiles = _bathymetry()
    for slug, entry in profiles.items():
        prov = entry["provenance"]
        surveys = [c["survey"] for c in prov["forecast_point_contributors"]]
        if "forecast_point_depth_m" in entry:
            assert prov["forecast_point_survey_share"] >= 0.5, slug
            assert surveys and "NBS Generalization" not in surveys[:1], slug
            assert "(survey " in entry["notes"], slug
        else:
            skipped = prov["not_applied"]["forecast_point_depth_m"]
            assert "NBS Generalization" in skipped["reason"] and skipped["value"] > 0
            assert prov["forecast_point_survey_share"] < 0.5
    assert "forecast_point_depth_m" not in profiles["trestles"]
    assert profiles["trestles"]["provenance"]["not_applied"]["forecast_point_depth_m"][
        "value"
    ] == pytest.approx(758.7)
    for slug, depth in (("mavericks", 57.9), ("ocean-beach-sf", 36.4), ("rincon", 96.5)):
        assert profiles[slug]["forecast_point_depth_m"] == pytest.approx(depth)


def test_every_bluetopo_tile_is_a_read_survey_source_with_a_verbatim_excerpt() -> None:
    profiles = _bathymetry()
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


# --------------------------------------------------------------------------- wind profiles
def test_committed_wind_profiles_cite_directly_read_claims() -> None:
    """Every wind profile is "research", validates against the evidence file and quotes a
    verbatim excerpt of a directly read source that it cites."""
    profiles = _load(PROFILES)
    evidence = _load(EVIDENCE)["spots"]
    retrievals = evidence_retrievals(DEFAULT_DATA_DIR)
    assert retrievals is not None
    winds = {slug: e["wind_profile"] for slug, e in profiles.items() if "wind_profile" in e}
    assert set(winds) == {
        "burleigh-heads",
        "g-land",
        "mundaka",
        "ocean-beach-sf",
        "padang-padang",
        "punta-de-lobos",
    }
    for slug, raw in winds.items():
        assert validate_profile(slug, {"wind_profile": raw}, retrievals[slug]) == []
        assert raw["data_quality"] == "research"
        keys = cited_keys(raw)
        assert keys, slug
        sources = {s["key"]: s for s in evidence[slug]["sources"]}
        for key in keys:
            assert sources[key]["retrieval"] == "fetched"
            excerpts = [c["excerpt"] for c in sources[key]["claims"] if c.get("excerpt")]
            # The notes quote the excerpt the value rests on.
            assert any(e in raw["notes"] for e in excerpts), (slug, key)
        profile = wind_profile_from_dict(raw)
        assert profile is not None and profile.data_quality == "research"
        for sector in profile.shelter:
            assert sector.factor == 0.5  # the one heuristic strength for "some shelter"
            assert (sector.from_max - sector.from_min) % 360 == 45.0
    assert winds["padang-padang"]["offshore_tolerance_kmh"] == 39.0


def _seed_files(tmp_path: Path, wind_profile: Any) -> Path:
    (tmp_path / "physical_profiles.json").write_text(
        json.dumps({"burleigh-heads": {"wind_profile": wind_profile}})
    )
    (tmp_path / "research_evidence.json").write_text(
        json.dumps(
            {
                "spots": {
                    "burleigh-heads": {
                        "sources": [
                            {"key": "read-source", "retrieval": "fetched"},
                            {"key": "search-only", "retrieval": "web_search"},
                        ]
                    }
                }
            }
        )
    )
    return tmp_path


GOOD_WIND_PROFILE: dict[str, Any] = {
    "offshore_tolerance_kmh": None,
    "sources": [],
    "shelter": [{"from_min": 157.5, "from_max": 202.5, "factor": 0.5, "sources": ["read-source"]}],
    "extra_offshore_deg": [],
    "data_quality": "research",
    "notes": "synthetic",
}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {"shelter": [{**GOOD_WIND_PROFILE["shelter"][0], "factor": 1.5}]},
            "factor must be in [0, 1]",
        ),
        (
            {"shelter": [{**GOOD_WIND_PROFILE["shelter"][0], "from_min": 360}]},
            "from_min must be in [0, 360)",
        ),
        (
            {"shelter": [{**GOOD_WIND_PROFILE["shelter"][0], "from_max": -10}]},
            "from_max must be in [0, 360)",
        ),
        ({"gusts": 1}, "unknown keys ['gusts']"),
        ({"shelter": [{**GOOD_WIND_PROFILE["shelter"][0], "width": 3}]}, "unknown keys ['width']"),
        (
            {"shelter": [{**GOOD_WIND_PROFILE["shelter"][0], "sources": ["nope"]}]},
            "unknown evidence key 'nope'",
        ),
        (
            {"shelter": [{**GOOD_WIND_PROFILE["shelter"][0], "sources": ["search-only"]}]},
            "was not read directly",
        ),
        ({"shelter": [{**GOOD_WIND_PROFILE["shelter"][0], "sources": []}]}, "needs sources"),
        ({"offshore_tolerance_kmh": 39.0}, "needs sources"),
        ({"offshore_tolerance_kmh": -1}, "offshore_tolerance_kmh must be a number"),
        ({"extra_offshore_deg": [400]}, "extra_offshore_deg must be a list of degrees"),
        ({"data_quality": "measured"}, "data_quality must be one of"),
    ],
)
def test_seed_rejects_a_malformed_wind_profile(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    directory = _seed_files(tmp_path, {**GOOD_WIND_PROFILE, **change})
    with pytest.raises(SeedValidationError, match=re.escape(message)):
        seed_physical_profiles(None, directory)  # type: ignore[arg-type]  # fails before any query


def test_a_valid_wind_profile_passes_validation(tmp_path: Path) -> None:
    directory = _seed_files(tmp_path, GOOD_WIND_PROFILE)
    retrievals = evidence_retrievals(directory)
    assert retrievals is not None
    assert (
        validate_profile(
            "burleigh-heads", {"wind_profile": GOOD_WIND_PROFILE}, retrievals["burleigh-heads"]
        )
        == []
    )
    # A heuristic default (e.g. an owner's setting) needs no sources.
    heuristic = {
        **GOOD_WIND_PROFILE,
        "data_quality": "heuristic default",
        "shelter": [],
        "offshore_tolerance_kmh": 33.0,
    }
    assert validate_profile("x", {"wind_profile": heuristic}, {}) == []
    assert validate_profile("x", {"wind_profile": "strong"}) == [
        "x: wind_profile must be an object"
    ]
