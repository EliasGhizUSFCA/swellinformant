"""docs/surf_spot_research/ and data/surf_spots/research_conditions.json (spec section 17).

The committed docs must be exactly what scripts/generate_spot_research_docs.py writes from
the committed data (a doc that is out of date fails here), every citation of
research_conditions.json must point at a real evidence claim with its exact excerpt (the
mechanical check), and every spot of spots.json has a doc. The mutation tests use SYNTHETIC
edits of an in-memory copy to show the check catches each kind of broken citation.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "generate_spot_research_docs.py"
DOCS = ROOT / "docs" / "surf_spot_research"
SPOTS = json.loads((ROOT / "data" / "surf_spots" / "spots.json").read_text(encoding="utf-8"))


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_spot_research_docs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    return load()


@pytest.fixture(scope="module")
def inputs(gen: ModuleType) -> dict[str, Any]:
    return gen.load_inputs()


def test_generated_docs_equal_the_committed_docs(gen: ModuleType, tmp_path: Path) -> None:
    assert gen.main(["--out", str(tmp_path)]) == 0
    generated = sorted(p.name for p in tmp_path.iterdir())
    committed = sorted(p.name for p in DOCS.iterdir())
    assert generated == committed
    stale = [
        name
        for name in generated
        if (tmp_path / name).read_text("utf-8") != (DOCS / name).read_text("utf-8")
    ]
    assert stale == [], f"out of date, run {gen.SCRIPT}: {stale}"
    assert gen.main(["--check"]) == 0


def test_the_output_is_deterministic(gen: ModuleType, inputs: dict[str, Any]) -> None:
    assert gen.generate(inputs) == gen.generate(copy.deepcopy(inputs))


def test_mechanical_citation_check_passes_for_all_50_spots(
    gen: ModuleType, inputs: dict[str, Any]
) -> None:
    assert len(SPOTS) == 50
    assert set(inputs["conditions"]["spots"]) == {s["slug"] for s in SPOTS}
    assert gen.check_conditions(inputs) == []


def test_every_spot_has_a_doc_and_an_index_row(gen: ModuleType) -> None:
    index = (DOCS / "README.md").read_text("utf-8")
    for spot in SPOTS:
        doc = DOCS / f"{spot['slug']}.md"
        assert doc.is_file(), spot["slug"]
        text = doc.read_text("utf-8")
        assert text.startswith(f"# {spot['name']}\n")
        for heading in (
            "## Summary",
            "## Ideal conditions",
            "## Conflicting information",
            "## Missing information",
            "## Model settings the evidence does not support",
            "## Sources",
        ):
            assert heading in text, (spot["slug"], heading)
        assert "| Research confidence |" in text and "| Forecast confidence |" in text
        assert "| Last review |" in text and "| Extraction |" in text
        assert f"]({spot['slug']}.md)" in index


def test_every_value_is_cited_or_marked_not_documented(inputs: dict[str, Any]) -> None:
    counts = {"documented": 0, "conflicting": 0, "not_documented": 0}
    for entry in inputs["conditions"]["spots"].values():
        for group, fields in entry["fields"].items():
            for name, f in fields.items():
                if (group, name) == ("tide", "local_tide_data"):
                    continue
                counts[f["status"]] += 1
                assert (f["value"] is None) == (f["status"] == "not_documented")
                assert bool(f["citations"]) == (f["status"] != "not_documented")
    assert sum(counts.values()) == 50 * 28
    assert all(counts.values())


def test_research_confidence_is_the_apps_class(gen: ModuleType, inputs: dict[str, Any]) -> None:
    # The docs show assess.compute_assessment's class for the committed evidence and profile.
    from datetime import UTC, datetime

    assessed = gen.assessments(inputs, datetime(2026, 10, 27, tzinfo=UTC))
    assert set(assessed) == {s["slug"] for s in SPOTS}
    for slug, a in assessed.items():
        doc = (DOCS / f"{slug}.md").read_text("utf-8")
        assert f"| Research confidence | **{a.overall_class.capitalize()}**" in doc


def _mutated(inputs: dict[str, Any], slug: str = "pipeline") -> tuple[dict[str, Any], Any]:
    data = copy.deepcopy(inputs)
    return data, data["conditions"]["spots"][slug]["fields"]


def _first_cited(fields: Any) -> tuple[str, str]:
    for group, names in fields.items():
        for name, f in names.items():
            if f["status"] == "documented" and f["citations"]:
                return group, name
    raise AssertionError("no documented field")


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("excerpt", "excerpt differs"),
        ("claim_index", "has no claim"),
        ("source_key", "is not a source of this spot"),
        ("drop_citations", "documented without a citation"),
        ("not_documented_with_citation", "not_documented but cites"),
        ("conflict_source", "sources not cited"),
        ("tide", "differs from data/tide_stations.json"),
    ],
)
def test_the_check_catches_broken_citations(
    gen: ModuleType, inputs: dict[str, Any], mutation: str, expected: str
) -> None:
    data, fields = _mutated(inputs)
    group, name = _first_cited(fields)
    field = fields[group][name]
    if mutation == "excerpt":
        field["citations"][0]["excerpt"] += " (paraphrased)"
    elif mutation == "claim_index":
        field["citations"][0]["claim_index"] = 999
    elif mutation == "source_key":
        field["citations"][0]["source_key"] = "synthetic-unknown-source"
    elif mutation == "drop_citations":
        field["citations"] = []
    elif mutation == "not_documented_with_citation":
        field["status"] = "not_documented"
        field["value"] = None
    elif mutation == "conflict_source":
        conflicting = next(
            f for names in fields.values() for f in names.values() if f["status"] == "conflicting"
        )
        conflicting["value"][0]["sources"] = ["synthetic-uncited-source"]
    elif mutation == "tide":
        fields["tide"]["local_tide_data"]["value"]["links"][0]["distance_km"] += 1.0
    problems = gen.check_conditions(data)
    assert any(expected in p for p in problems), problems
    with pytest.raises(ValueError, match="citation check"):
        gen.generate(data)
