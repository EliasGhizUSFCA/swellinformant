"""Forecast-confidence reasons are kept apart from research and wind-dispute text (CT-1).

``forecast_confidence_reasons`` holds the forecast rules only; the research sentence is the
local reason and the disputed-wind note has its own field. The deprecated mixed
``confidence_reasons`` list is unchanged. Every input is a SYNTHETIC test value.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.surf_quality import forecast_confidence as fc
from app.services.surf_quality.forecast_confidence import (
    HIGH,
    LOW,
    MODERATE,
    Disagreement,
    EnsembleSignal,
    ForecastInputs,
    ResearchBasisInput,
    classify_forecast,
    classify_local,
)
from app.services.swell_detection.service import peak_confidence

FORBIDDEN = {*fc.RESEARCH_REASONS.values(), fc.WIND_DISPUTE_REASON, fc.NOT_VERIFIED_REASON}
DISPUTE = {"reason": fc.WIND_DISPUTE_REASON, "relation": "offshore", "differs": True}


def research(cls: str) -> ResearchBasisInput:
    return ResearchBasisInput(
        assessment_id=1,
        overall_class=cls,
        variable_classes={v: cls for v in fc.RESEARCH_BASIS_VARIABLES},
    )


def stored(
    *, lead: float, index: int, research_cls: str | None, dispute: bool, stale: bool = False
) -> SimpleNamespace:
    out = classify_forecast(
        ForecastInputs(
            index=index,
            sigma_source="lead_table",
            lead_hours=lead,
            source_code="blend",
            families=("ncep_gfs", "ecmwf_ifs"),
            ensemble=EnsembleSignal(2.0, 0.2),
            disagreement=Disagreement(hs_log_ratio=0.05),
            stale=stale,
        )
    )
    local = classify_local(out.cls, research(research_cls) if research_cls else None)
    inputs = fc.confidence_inputs(out, local, DISPUTE if dispute else None)
    return SimpleNamespace(
        forecast_confidence_class=out.cls,
        local_confidence_class=local.cls,
        confidence_inputs=inputs,
        lead_hours=lead,
    )


CASES = list(
    itertools.product(
        (12.0, 48.0, 120.0, 200.0, 260.0),
        (90, 60, 30),
        (None, LOW, MODERATE, HIGH),
        (False, True),
        (False, True),
    )
)


@pytest.mark.parametrize(("lead", "index", "research_cls", "dispute", "stale"), CASES)
def test_forecast_reasons_never_carry_research_or_dispute_text(
    lead: float, index: int, research_cls: str | None, dispute: bool, stale: bool
) -> None:
    p = stored(lead=lead, index=index, research_cls=research_cls, dispute=dispute)
    fields = fc.api_fields(p, stale=stale)
    reasons = fields["forecast_confidence_reasons"]
    assert not FORBIDDEN & set(reasons)
    assert reasons, "a forecast class always has at least one forecast reason"
    # Every forecast reason is one of the hour's own forecast-rule reasons (or the
    # read-time stale reason): nothing is invented.
    allowed = set(p.confidence_inputs["reasons"]) | {fc.STALE_REASON}
    assert set(reasons) <= allowed
    # The disputed-wind note has its own field; the deprecated list is unchanged.
    assert fields["wind_dispute_note"] == (fc.WIND_DISPUTE_REASON if dispute else None)
    mixed = fc.stored_reasons(p.confidence_inputs)
    expected_mixed = [fc.STALE_REASON, *mixed] if stale and fc.STALE_REASON not in mixed else mixed
    assert fields["confidence_reasons"] == expected_mixed
    assert (fc.WIND_DISPUTE_REASON in fields["confidence_reasons"]) is dispute


def test_stale_reason_comes_first_when_the_read_time_cap_applies() -> None:
    p = stored(lead=48.0, index=90, research_cls=HIGH, dispute=True)
    fresh = fc.api_fields(p)
    assert fc.STALE_REASON not in fresh["forecast_confidence_reasons"]
    capped = fc.api_fields(p, stale=True)
    assert capped["forecast_confidence_reasons"][0] == fc.STALE_REASON
    assert capped["forecast_confidence_reasons"][1:] == fresh["forecast_confidence_reasons"]
    assert capped["forecast_confidence_class"] == MODERATE
    # Stale already at scoring time: the reason is moved to the front, not repeated.
    scored_stale = stored(lead=48.0, index=90, research_cls=HIGH, dispute=False, stale=True)
    again = fc.api_fields(scored_stale, stale=True)["forecast_confidence_reasons"]
    assert again[0] == fc.STALE_REASON and again.count(fc.STALE_REASON) == 1


def test_local_reason_only_when_local_is_lower_than_forecast() -> None:
    """The stored combined class (kept for the archive) and its reason; spec 20 (2.0): the
    API serves neither, only the separate research confidence."""
    for research_cls in (None, LOW, MODERATE, HIGH):
        p = stored(lead=24.0, index=90, research_cls=research_cls, dispute=False)
        fields = fc.api_fields(p)
        reason = fc.local_reason(
            p.forecast_confidence_class, p.local_confidence_class, p.confidence_inputs
        )
        lower = fc.rank(p.local_confidence_class) < fc.rank(fields["forecast_confidence_class"])
        if lower:
            assert reason in fc.RESEARCH_REASONS.values()
        else:
            assert reason is None
        assert reason not in fields["forecast_confidence_reasons"]
        assert "local_confidence_class" not in fields and "local_confidence_reason" not in fields
        assert fields["research_confidence"] == research_cls


def test_the_constant_not_verified_note_stays_in_the_stored_and_mixed_lists_only() -> None:
    p = stored(lead=24.0, index=90, research_cls=HIGH, dispute=False)
    assert fc.NOT_VERIFIED_REASON in p.confidence_inputs["reasons"]
    fields = fc.api_fields(p)
    assert fc.NOT_VERIFIED_REASON in fields["confidence_reasons"]
    assert fc.NOT_VERIFIED_REASON not in fields["forecast_confidence_reasons"]


def test_older_event_snapshots_get_the_split_from_their_mixed_list() -> None:
    """Events detected before the split stored only ``confidence_reasons``."""
    mixed = [
        "A 6-day forecast, and ensemble members do not agree closely.",
        fc.NOT_VERIFIED_REASON,
        fc.WIND_DISPUTE_REASON,
        fc.RESEARCH_REASONS[LOW],
    ]
    snapshot: dict[str, Any] = {
        "peak_score": 70,
        "forecast_confidence_class": HIGH,
        "local_confidence_class": LOW,
        "confidence_reasons": mixed,
        "local_confidence_reason": fc.RESEARCH_REASONS[LOW],
    }
    event = SimpleNamespace(history=[snapshot])
    fields = peak_confidence(event)  # type: ignore[arg-type]
    assert fields["forecast_confidence_reasons"] == [mixed[0]]
    assert fields["wind_dispute_note"] == fc.WIND_DISPUTE_REASON
    assert fields["confidence_reasons"] == mixed
    capped = peak_confidence(event, stale=True)  # type: ignore[arg-type]
    assert capped["forecast_confidence_reasons"] == [fc.STALE_REASON, mixed[0]]
    assert capped["forecast_confidence_class"] == MODERATE
    # Their combined local class (spec 20) is not read back.
    assert not {"local_confidence_class", "local_confidence_reason"} & (fields | capped).keys()
    without = peak_confidence(  # type: ignore[arg-type]
        SimpleNamespace(history=[{**snapshot, "confidence_reasons": mixed[:2]}])
    )
    assert without["wind_dispute_note"] is None
    # New snapshots carry the split themselves and are passed through.
    p = stored(lead=24.0, index=90, research_cls=LOW, dispute=True)
    new = {"peak_score": 70, **fc.api_fields(p)}
    out = peak_confidence(SimpleNamespace(history=[new]))  # type: ignore[arg-type]
    assert out["forecast_confidence_reasons"] == fc.api_fields(p)["forecast_confidence_reasons"]
    assert out["wind_dispute_note"] == fc.WIND_DISPUTE_REASON
