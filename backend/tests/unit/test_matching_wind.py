"""Matching's wind requirements after algorithm 2.1.0 (owner decision: direction classes keep
their meaning; strong offshore is penalised through the score, not by redefining "offshore").

Every hour here is SYNTHETIC.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.models.enums import WindRequirement
from app.services.matching import evaluate as matching
from app.services.matching.evaluate import SearchCriteria, step_meets, wind_meets
from app.services.surf_quality.models import SeaState, SpotParams
from app.services.surf_quality.scoring import evaluate, min_score_for_label

JBAY = SpotParams(
    slug="jbay",
    latitude=-34.03,
    longitude=24.93,
    swell_window_min=180,
    swell_window_max=250,
    optimal_swell_direction_min=200,
    optimal_swell_direction_max=230,
    offshore_wind_direction=290,
    min_swell_period_s=11,
    ideal_swell_period_s=15,
    wave_height_min_ft=4,
    wave_height_max_ft=12,
    height_factor=0.9,
)


def hour(speed: float, direction: float) -> tuple[Any, Any]:
    """A scored hour (prediction, forecast row) with a well-aligned 2 m, 16 s swell."""
    sea = SeaState(
        primary_swell_height_m=2.0,
        primary_swell_period_s=16.0,
        primary_swell_direction_deg=215.0,
        wind_speed_kmh=speed,
        wind_direction_deg=direction,
    )
    r = evaluate(sea, JBAY)
    pred = SimpleNamespace(
        score=r.score,
        breaking_height_mid_ft=r.breaking.mid_ft,
        breaking_height_min_ft=r.breaking.min_ft,
        breaking_height_max_ft=r.breaking.max_ft,
        wind_relation=r.wind_relation,
        components=r.stored_components(),
    )
    fc = SimpleNamespace(
        primary_swell_period_s=16.0,
        wave_period_s=None,
        wind_speed_kmh=speed,
        primary_swell_direction_deg=215.0,
    )
    return pred, fc


def criteria(requirement: WindRequirement, min_label: str = "fair") -> SearchCriteria:
    return SearchCriteria(
        wave_min_ft=1.0,
        wave_max_ft=30.0,
        min_score=min_score_for_label(min_label),
        wind_requirement=requirement,
    )


def test_strong_offshore_counts_as_offshore_but_scores_lower() -> None:
    pred, fc = hour(60.0, 290.0)
    assert pred.wind_relation == "offshore"
    assert pred.components["wind_detail"]["strength"] == "strong"
    # The direction class still satisfies an "offshore" search ...
    assert step_meets(pred, fc, criteria(WindRequirement.OFFSHORE))
    # ... but the score is no longer Very Good, so a Very Good search does not match it.
    assert not step_meets(pred, fc, criteria(WindRequirement.OFFSHORE, "very_good"))
    light_pred, light_fc = hour(10.0, 290.0)
    assert step_meets(light_pred, light_fc, criteria(WindRequirement.OFFSHORE, "very_good"))


def test_light_onshore_is_still_onshore() -> None:
    pred, fc = hour(6.0, 110.0)
    assert pred.wind_relation == "onshore"
    assert pred.components["wind_detail"]["strength"] == "light"
    # Algorithm 2.2.0 (spec 19, light onshore is often marginal): at best "workable".
    assert pred.components["wind_detail"]["favourability"] == "workable"
    assert pred.components["wind_detail"]["onshore_capped"] is True
    assert not step_meets(pred, fc, criteria(WindRequirement.NOT_ONSHORE))
    assert step_meets(pred, fc, criteria(WindRequirement.ANY))


def test_optional_rules_are_off_by_default_and_work_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert matching.EXCLUDE_STRONG_OFFSHORE is False
    assert matching.LIGHT_ONSHORE_IS_NOT_ONSHORE is False
    monkeypatch.setattr(matching, "EXCLUDE_STRONG_OFFSHORE", True)
    monkeypatch.setattr(matching, "LIGHT_ONSHORE_IS_NOT_ONSHORE", True)
    assert not wind_meets("offshore", 60.0, WindRequirement.OFFSHORE)
    assert not wind_meets("cross-offshore", 29.0, WindRequirement.OFFSHORE)
    assert wind_meets("offshore", 28.9, WindRequirement.OFFSHORE)
    assert wind_meets("glassy", 3.0, WindRequirement.OFFSHORE)
    assert wind_meets("onshore", 6.0, WindRequirement.NOT_ONSHORE)
    assert wind_meets("cross-onshore", 11.9, WindRequirement.NOT_ONSHORE)
    assert not wind_meets("onshore", 12.0, WindRequirement.NOT_ONSHORE)
    pred, fc = hour(6.0, 110.0)
    assert step_meets(pred, fc, criteria(WindRequirement.NOT_ONSHORE))


@pytest.mark.parametrize(
    ("relation", "offshore_ok", "not_onshore_ok"),
    [
        ("glassy", True, True),
        ("offshore", True, True),
        ("cross-offshore", True, True),
        ("cross-shore", False, True),
        ("cross-onshore", False, False),
        ("onshore", False, False),
        ("unknown", False, True),
    ],
)
def test_direction_classes(relation: str, offshore_ok: bool, not_onshore_ok: bool) -> None:
    assert wind_meets(relation, 15.0, WindRequirement.OFFSHORE) is offshore_ok
    assert wind_meets(relation, 15.0, WindRequirement.NOT_ONSHORE) is not_onshore_ok
    assert wind_meets(relation, 15.0, WindRequirement.ANY) is True
