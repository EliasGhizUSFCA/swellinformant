"""Research evidence sync, assessment history, API output and the ``research`` CLI.

All evidence is SYNTHETIC (tests/research_synthetic.py), attached to real spot slugs only
so that it can be compared with their curated profiles.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session

from app.cli import main as cli_main
from app.core.database import get_engine
from app.models import SpotResearchAssessment, SpotResearchClaim, SpotResearchSource, SurfSpot
from app.services.research import assess, current_assessments
from app.services.research.assess import assess_all, assess_spot
from app.services.research.evidence import (
    EVIDENCE_FILENAME,
    EvidenceValidationError,
    load_evidence,
    sync_evidence,
)
from app.services.seed import DEFAULT_DATA_DIR, SeedValidationError, seed_reference_data
from app.services.spot_views import spot_summaries
from tests.conftest import ApiClient
from tests.research_synthetic import (
    AGREEING_VALUES,
    synthetic_claim,
    synthetic_file,
    synthetic_source,
)

pytestmark = pytest.mark.integration


def _pipeline_sources() -> list[dict[str, Any]]:
    claims = [
        synthetic_claim(v, AGREEING_VALUES[v], True)
        for v in ("swell_direction", "swell_size", "wind", "bathymetry", "local_behavior")
    ]
    claims.append(synthetic_claim("tide", {"tide": "all"}, True))  # Pipeline is listed "all"
    return [
        synthetic_source("survey", "survey_data", list(claims), year=2021),
        synthetic_source("guide", "guidebook", list(claims), year=2018),
        synthetic_source(
            "forum",
            "community",
            # Researcher says "agrees", but Jun-Jul is outside the listed Nov-Mar.
            [synthetic_claim("seasonality", {"months": [6, 7]}, True)],
            year=None,
        ),
    ]


def _write(path: Path, raw: dict[str, Any]) -> Path:
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def _evidence_file(tmp_path: Path, spots: dict[str, list[dict[str, Any]]] | None = None) -> Path:
    spots = spots if spots is not None else {"pipeline": _pipeline_sources()}
    return _write(tmp_path / EVIDENCE_FILENAME, synthetic_file(spots))


def _spot(db: Session, slug: str) -> SurfSpot:
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug))
    assert spot is not None
    return spot


def _snapshot(db: Session) -> list[tuple[Any, ...]]:
    sources = db.execute(
        select(
            SpotResearchSource.id, SpotResearchSource.source_key, SpotResearchSource.title
        ).order_by(SpotResearchSource.id)
    ).all()
    claims = db.execute(
        select(SpotResearchClaim.id, SpotResearchClaim.claim).order_by(SpotResearchClaim.id)
    ).all()
    return [tuple(r) for r in sources] + [tuple(r) for r in claims]


def _clear_assessments(db: Session) -> None:
    """The session-wide seed may have left assessments; start from none."""
    db.execute(delete(SpotResearchAssessment))
    db.commit()


def test_missing_file_is_no_evidence(db: Session, tmp_path: Path) -> None:
    assert load_evidence(db, tmp_path / "missing.json") is None
    counts = sync_evidence(db, None)
    assert counts["sources_created"] == 0 and counts["sources_removed"] == 0


def test_invalid_file_raises_with_every_problem(db: Session, tmp_path: Path) -> None:
    raw = synthetic_file(
        {
            "pipeline": [synthetic_source("a", "rumour", [synthetic_claim("tide")])],
            "atlantis": [],
        }
    )
    with pytest.raises(EvidenceValidationError) as info:
        load_evidence(db, _write(tmp_path / "bad.json", raw))
    assert isinstance(info.value, SeedValidationError)
    assert any("atlantis: unknown spot slug" in p for p in info.value.problems)
    assert any("source_type 'rumour'" in p for p in info.value.problems)
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(EvidenceValidationError):
        load_evidence(db, tmp_path / "broken.json")


def test_sync_is_idempotent_and_follows_the_file(db: Session, tmp_path: Path) -> None:
    path = _evidence_file(
        tmp_path,
        {
            "pipeline": _pipeline_sources(),
            "supertubos": [synthetic_source("s1", "guidebook", [synthetic_claim("wind")])],
        },
    )
    first = sync_evidence(db, load_evidence(db, path))
    db.commit()
    assert first["sources_created"] == 4 and first["evidence_spots"] == 2
    before = _snapshot(db)
    again = sync_evidence(db, load_evidence(db, path))
    db.commit()
    assert again["sources_created"] == again["sources_updated"] == again["sources_removed"] == 0
    assert again["claim_sets_replaced"] == 0 and again["sources_unchanged"] == 4
    assert _snapshot(db) == before  # same ids, nothing rewritten

    # Change one claim, retitle one source, drop one source and the second spot.
    sources = _pipeline_sources()
    sources[0]["claims"][0] = synthetic_claim("swell_direction", {"min": 280, "max": 330}, True)
    sources[1]["title"] = "SYNTHETIC retitled source"
    path = _evidence_file(tmp_path, {"pipeline": sources[:2]})
    changed = sync_evidence(db, load_evidence(db, path))
    db.commit()
    assert changed["sources_updated"] == 2 and changed["claim_sets_replaced"] == 1
    assert changed["sources_removed"] == 2  # forum (pipeline) and s1 (supertubos)
    keys = db.scalars(select(SpotResearchSource.source_key).order_by(SpotResearchSource.source_key))
    assert list(keys) == ["guide", "survey"]
    n_claims = db.scalar(select(func.count()).select_from(SpotResearchClaim))
    assert n_claims == 12  # 6 + 6; claims of removed sources are gone (cascade)


def test_assessment_history(db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_assessments(db)
    pipeline = _spot(db, "pipeline")
    sync_evidence(db, load_evidence(db, _evidence_file(tmp_path)))
    first, created = assess_spot(db, pipeline)
    db.commit()
    assert created and first.is_current and first.methodology_version == "research-v1"
    assert first.overall_class == "high"
    season = first.per_variable["seasonality"]
    assert season["class"] == "low" and season["agreement"]["contradicting"] == 1
    assert season["flag_differences"] == 1  # the researcher's flag differs; kept in details
    assert first.research_updated_at is not None and first.research_updated_at.year == 2026

    # Unchanged evidence: no new row, the current one is returned.
    same, created = assess_spot(db, pipeline)
    assert not created and same.id == first.id
    counts = assess_all(db, slug="pipeline")
    assert counts == {
        "assessed": 1,
        "created": 0,
        "unchanged": 1,
        "high": 1,
        "moderate": 0,
        "low": 0,
    }

    # New evidence: a new current row; the old one is kept and no longer current.
    sources = _pipeline_sources()[:1]  # only the survey remains
    sync_evidence(db, load_evidence(db, _evidence_file(tmp_path, {"pipeline": sources})))
    second, created = assess_spot(db, pipeline)
    db.commit()
    assert created and second.id != first.id and second.overall_class == "moderate"
    db.refresh(first)
    assert first.is_current is False and first.overall_class == "high"

    # A new methodology version also inserts a new row, even with unchanged evidence.
    monkeypatch.setattr(assess, "METHODOLOGY_VERSION", "research-test")
    third, created = assess_spot(db, pipeline)
    db.commit()
    assert created and third.methodology_version == "research-test"
    rows = db.scalars(
        select(SpotResearchAssessment)
        .where(SpotResearchAssessment.spot_id == pipeline.id)
        .order_by(SpotResearchAssessment.id)
    ).all()
    assert [r.is_current for r in rows] == [False, False, True]
    assert current_assessments(db)[pipeline.id].id == third.id

    # --force inserts a new row although nothing changed.
    forced, created = assess_spot(db, pipeline, force=True)
    db.commit()
    assert created and forced.evidence_fingerprint == third.evidence_fingerprint


def test_profile_change_creates_a_new_assessment(db: Session, tmp_path: Path) -> None:
    _clear_assessments(db)
    pipeline = _spot(db, "pipeline")
    sync_evidence(db, load_evidence(db, _evidence_file(tmp_path)))
    first, _ = assess_spot(db, pipeline)
    db.commit()
    pipeline.offshore_wind_direction = 300.0  # synthetic edit: now the wind claims disagree
    db.flush()
    second, created = assess_spot(db, pipeline)
    assert created and second.per_variable["wind"]["class"] == "low"
    assert second.overall_class == "low"
    db.rollback()


def test_seed_loads_evidence_and_assesses_every_spot(db: Session, tmp_path: Path) -> None:
    _clear_assessments(db)
    data_dir = tmp_path / "surf_spots"
    data_dir.mkdir()
    for name in ("spots.json", "airports.json"):
        shutil.copy(DEFAULT_DATA_DIR / name, data_dir / name)
    _evidence_file(data_dir)
    out = seed_reference_data(db, data_dir)
    assert out["research_evidence_spots"] == 1 and out["research_sources_created"] == 3
    n_spots = db.scalar(select(func.count()).select_from(SurfSpot))
    current = current_assessments(db)
    assert len(current) == n_spots == 50
    assert current[_spot(db, "pipeline").id].overall_class == "high"
    nazare = current[_spot(db, "nazare").id]
    assert nazare.overall_class == "low" and nazare.research_updated_at is None
    assert nazare.per_variable["tide"]["gaps"] == [
        "Limited information is available about this break's preferred tide conditions."
    ]
    # Seeding again changes nothing.
    again = seed_reference_data(db, data_dir)
    assert again["research_assessments_created"] == 0 and again["research_sources_created"] == 0
    total = db.scalar(select(func.count()).select_from(SpotResearchAssessment))
    assert total == 50


def test_api_detail_and_summaries(app_client: ApiClient, db: Session, tmp_path: Path) -> None:
    _clear_assessments(db)
    detail = app_client.get("/api/spots/pipeline").json()
    assert detail["research"] is None and detail["research_class"] is None

    sync_evidence(db, load_evidence(db, _evidence_file(tmp_path)))
    assess_all(db)
    db.commit()
    spots = app_client.get("/api/spots").json()
    assert len(spots) == 50 and all(
        s["research_class"] in ("high", "moderate", "low") for s in spots
    )
    by_slug = {s["slug"]: s for s in spots}
    assert by_slug["pipeline"]["research_class"] == "high"
    assert by_slug["nazare"]["research_class"] == "low"

    detail = app_client.get("/api/spots/pipeline").json()
    research = detail["research"]
    assert detail["research_class"] == research["research_class"] == "high"
    assert research["methodology_version"] == "research-v1"
    assert [v["label"] for v in research["variables"]] == [
        "Swell direction",
        "Swell size",
        "Swell period",
        "Wind",
        "Tide",
        "Bottom / bathymetry",
        "Season",
        "How the wave breaks",
    ]
    season = next(v for v in research["variables"] if v["variable"] == "seasonality")
    assert season["research_class"] == "low" and season["source_count"] == 1
    assert season["gaps"] == ["The sources found do not support this break's listed best months."]
    period = next(v for v in research["variables"] if v["variable"] == "swell_period")
    assert period["source_count"] == 0 and period["gaps"] == [
        "Limited information is available about the swell period this break needs."
    ]
    assert len(research["sources"]) == 3
    assert research["sources"][0]["source_type"] == "survey_data"  # strongest first
    assert all("excerpt" not in s for s in research["sources"])
    assert "SYNTHETIC excerpt" not in json.dumps(research)
    assert research["research_updated_at"].startswith("2026-10-01")
    empty = app_client.get("/api/spots/nazare").json()["research"]
    assert empty["research_class"] == "low" and empty["sources"] == []
    assert all(v["source_count"] == 0 for v in empty["variables"])


def test_summaries_read_research_in_one_query(db: Session) -> None:
    _clear_assessments(db)
    assess_all(db)
    db.commit()
    db.expunge_all()
    statements: list[str] = []

    def count(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", count)
    try:
        summaries = spot_summaries(db)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(summaries) == 50 and all(s.research_class == "low" for s in summaries)
    assert sum("spot_research_assessments" in s for s in statements) == 1
    assert len(statements) < 15  # no per-spot queries for 50 spots


def test_cli(db: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _clear_assessments(db)  # also ends the transaction: the CLI uses its own session
    path = _evidence_file(tmp_path)
    assert cli_main(["research", "load", "--path", str(path)]) == 0
    loaded = json.loads(capsys.readouterr().out)
    assert loaded["sources_created"] == 3 and loaded["assessed"] == 50

    assert cli_main(["research", "show", "pipeline"]) == 0
    out = capsys.readouterr().out
    assert "Research confidence: HIGH" in out and "not forecast confidence" in out
    assert "Swell direction" in out and "SYNTHETIC test source survey" in out
    assert "gap: The sources found do not support this break's listed best months." in out

    assert cli_main(["research", "assess", "--slug", "pipeline"]) == 0
    assert json.loads(capsys.readouterr().out)["created"] == 0
    assert cli_main(["research", "assess", "--slug", "pipeline", "--force"]) == 0
    assert json.loads(capsys.readouterr().out)["created"] == 1
    assert cli_main(["research", "assess", "--slug", "atlantis"]) == 1
    assert cli_main(["research", "show", "atlantis"]) == 1

    bad = _write(tmp_path / "bad.json", synthetic_file({"atlantis": []}))
    assert cli_main(["research", "load", "--path", str(bad)]) == 2
    assert "atlantis: unknown spot slug" in capsys.readouterr().err
    assert cli_main(["research", "load", "--path", str(tmp_path / "nope.json")]) == 1
