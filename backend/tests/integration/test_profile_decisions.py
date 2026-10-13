"""Measured physical profiles in the database and ``research lint`` over the REAL data files
(spots.json, physical_profiles.json, research_evidence.json, profile_decisions.json)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.cli import main as cli_main
from app.models import SpotPhysicalProfile, SpotResearchAssessment, SurfSpot
from app.services.research.assess import seed_research
from app.services.seed import DEFAULT_DATA_DIR

pytestmark = pytest.mark.integration


def _profile(db: Session, slug: str) -> SpotPhysicalProfile:
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug))
    assert spot is not None
    row = db.get(SpotPhysicalProfile, spot.id)
    assert row is not None
    return row


def test_seeded_profiles_carry_the_measured_bathymetry(db: Session) -> None:
    trestles = _profile(db, "trestles")
    assert trestles.shore_normal_deg == pytest.approx(210.9)
    assert trestles.data_quality["shore_normal_deg"] == "measured"
    # Trestles' forecast point lies in NBS Generalization cells (no survey): its depth is
    # not a measurement, so it stays unknown (deep water assumed).
    assert trestles.forecast_point_depth_m is None
    assert trestles.data_quality["forecast_point_depth_m"] == "unknown"
    assert "BlueTopo" in trestles.notes and "NBS Generalization" in trestles.notes
    for slug, depth in (("mavericks", 57.9), ("ocean-beach-sf", 36.4), ("rincon", 96.5)):
        row = _profile(db, slug)
        assert row.forecast_point_depth_m == pytest.approx(depth)
        assert row.data_quality["forecast_point_depth_m"] == "measured"
        # The measured contour normal was not applied here: the estimate stays.
        assert row.data_quality["shore_normal_deg"] == "estimated"
        assert row.controlling_depth_m is None
    # A spot without measurements keeps the derived profile.
    assert _profile(db, "pipeline").data_quality["forecast_point_depth_m"] == "unknown"


@pytest.fixture
def assessed(db: Session) -> None:
    """The committed evidence assessed (the db fixture truncates research tables)."""
    db.execute(delete(SpotResearchAssessment))
    seed_research(db, DEFAULT_DATA_DIR)
    db.commit()


def test_research_lint_lists_only_unexplained_geometry(
    db: Session, assessed: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main(["research", "lint"]) == 1
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if ": [" in line]  # one line per issue
    # Restaurants: offshore 120 deg vs a SW-facing estimated shore normal, and no source
    # describes the reef wrapping, so no reason is recorded (left for review).
    assert lines == [
        "restaurants: [geometry] offshore wind 120 deg is 85 deg from straight offshore "
        "(35 deg, shore normal 215) with no recorded reason"
    ]
    assert "1 issue(s); 50 spots, 88 decision records" in out


def test_research_lint_reports_missing_records_and_file_problems(
    db: Session, assessed: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = json.loads((DEFAULT_DATA_DIR / "profile_decisions.json").read_text(encoding="utf-8"))
    without_wind = {
        **raw,
        "decisions": [r for r in raw["decisions"] if r["field"] != "offshore_wind_direction"],
    }
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(without_wind), encoding="utf-8")
    assert cli_main(["research", "lint", "--decisions", str(path)]) == 1
    out = capsys.readouterr().out
    assert "chicama: [missing_record] offshore_wind_direction" in out
    assert "jeffreys-bay: [geometry]" in out and "jeffreys-bay: [missing_record]" in out
    assert "trestles: [geometry]" not in out  # its measured shore normal fits the wind
    broken = {**raw, "decisions": [{**raw["decisions"][0], "old_value": 1.0}]}
    path.write_text(json.dumps(broken), encoding="utf-8")
    assert cli_main(["research", "lint", "--decisions", str(path)]) == 2
    assert "is not the record's" in capsys.readouterr().err
    assert cli_main(["research", "lint", "--decisions", str(tmp_path / "nope.json")]) == 1
