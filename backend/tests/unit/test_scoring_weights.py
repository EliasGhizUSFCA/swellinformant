"""SCORING_WEIGHTS_JSON: validated component weights whose defaults reproduce 2.1.0 exactly.

tests/fixtures/scoring/scores_2_1_0.json holds SYNTHETIC hours (seeded random sea states at
the 50 seeded spots, made up for this test) with the results algorithm 2.1.0 (commit
a00da14, before this package) gave them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.surf_quality import scoring
from app.services.surf_quality.models import SeaState, SpotParams
from app.services.surf_quality.weights import (
    DEFAULT_WEIGHTS,
    effective_weights,
    tagged_version,
    validate_weights,
    weights_tag,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scoring" / "scores_2_1_0.json"
REFERENCE: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = REFERENCE["cases"]
# The 2.1.0 constants (scoring.WEIGHTS before SCORING_WEIGHTS_JSON existed).
WEIGHTS_2_1_0 = {
    "swell_direction": 0.20,
    "swell_period": 0.20,
    "wave_size": 0.20,
    "wind": 0.30,
    "tide": 0.05,
    "consistency": 0.05,
}


def make(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def test_the_defaults_are_the_2_1_0_constants() -> None:
    assert dict(DEFAULT_WEIGHTS) == WEIGHTS_2_1_0 == REFERENCE["weights"]
    assert scoring.WEIGHTS == WEIGHTS_2_1_0
    assert REFERENCE["algorithm_version"] == "2.1.0" and len(CASES) == 160


def _result(case: dict[str, Any], weights: dict[str, float] | None) -> scoring.QualityResult:
    return scoring.evaluate(SeaState(**case["sea"]), SpotParams(**case["spot"]), weights=weights)


@pytest.mark.parametrize("weights", [None, "defaults", "json"])
def test_default_weights_reproduce_the_2_1_0_scores_exactly(weights: str | None) -> None:
    """Every stored 2.1.0 output (score, label, fits and gates, weights used, points lost,
    explanation) is reproduced exactly: with no weights, with the defaults given explicitly
    and with SCORING_WEIGHTS_JSON naming every default."""
    chosen: dict[str, float] | None = None
    if weights == "defaults":
        chosen = effective_weights(None)
    elif weights == "json":
        settings = make(scoring_weights_json=json.dumps(WEIGHTS_2_1_0))
        chosen = effective_weights(settings.scoring_weights)
    labels = set()
    for case in CASES:
        r = _result(case, chosen)
        assert r.score == case["score"], case["spot"]["slug"]
        assert r.label == case["label"]
        assert r.components == case["components"]
        assert r.details["weights_used"] == case["weights_used"]
        assert r.details["contributions"] == case["contributions"]
        assert r.explanation == case["explanation"]
        assert r.model_version == scoring.ALGORITHM_VERSION == "2.2.0"
        labels.add(r.label)
    assert {"poor", "fair", "good", "very_good", "excellent"} <= labels  # not vacuous


def test_an_override_changes_the_scores_and_tags_the_version() -> None:
    settings = make(scoring_weights_json='{"wind": 0.9, "tide": 0}')
    weights = effective_weights(settings.scoring_weights)
    assert weights == {**WEIGHTS_2_1_0, "wind": 0.9, "tide": 0.0}
    changed = 0
    for case in CASES:
        r = _result(case, weights)
        assert "tide" not in r.details["weights_used"] or r.details["weights_used"]["tide"] == 0
        changed += r.score != case["score"]
        assert r.model_version == tagged_version("2.2.0", weights)
    assert changed > len(CASES) // 4
    version = scoring.model_version(weights)
    assert version.startswith("2.2.0+w") and len(version) <= 16  # the column holds 16
    assert scoring.model_version(None) == scoring.model_version(dict(WEIGHTS_2_1_0)) == "2.2.0"
    assert weights_tag(weights) != weights_tag({**weights, "wind": 0.8})


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ('{"wind_speed": 0.3}', "unknown components"),
        ('{"tides": 0.1, "wind": 0.3}', "unknown components"),
        ('{"wave_size": 0}', "wave_size"),
        ('{"wind": -0.1}', ">= 0"),
        ('{"wind": "0.3"}', ">= 0"),
        ('{"wind": true}', ">= 0"),
        ('{"wind": 1e999}', ">= 0"),
        ("[0.2, 0.3]", "SCORING_WEIGHTS_JSON"),
        ("{not json", "SCORING_WEIGHTS_JSON"),
    ],
)
def test_invalid_weights_stop_the_settings_from_loading(value: str, message: str) -> None:
    """An invalid SCORING_WEIGHTS_JSON is a start-up error (the settings do not load)."""
    with pytest.raises(ValidationError, match="SCORING_WEIGHTS_JSON") as err:
        make(scoring_weights_json=value)
    assert message in str(err.value)


def test_validate_weights_names_every_component() -> None:
    assert validate_weights({}) == WEIGHTS_2_1_0
    assert validate_weights({"consistency": 0.1}) == {**WEIGHTS_2_1_0, "consistency": 0.1}
    with pytest.raises(ValueError, match="swell_direction"):
        validate_weights({"direction": 0.2})  # the message lists the real names
