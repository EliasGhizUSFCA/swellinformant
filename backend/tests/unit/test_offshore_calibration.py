"""The calibrated offshore component of the 80 % range (``ndbc-offshore-v1``).

The k factors under test are the values fitted on NDBC buoys (docs/ml-evaluation.md
section 12.2); every height and lead below is a SYNTHETIC input chosen for the test.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.services.surf_quality.uncertainty import (
    OFFSHORE_CALIBRATION,
    OFFSHORE_CALIBRATION_VERSION,
    SIGMA_FLOOR,
    Z80,
    EnsembleInfo,
    OffshoreContext,
    breaking_uncertainty,
    lead_sigma,
    offshore_range,
)
from app.services.verification import ranges

GFS_ONLY = OffshoreContext("trestles", ("ncep_gfs",), ml_corrected=False)


def test_without_a_context_nothing_changes() -> None:
    for lead in (6.0, 24.0, 100.0, 239.0):
        unc = breaking_uncertainty(5.0, lead)
        s_off = math.hypot(lead_sigma(lead), SIGMA_FLOOR)
        total = math.sqrt((0.8 * s_off) ** 2 + (0.4 * 0.08) ** 2 + 0.30**2)
        assert unc.p10_ft == round(5.0 * math.exp(-Z80 * total), 1)
        assert unc.p90_ft == round(5.0 * math.exp(Z80 * total), 1)
        assert unc.kind == unc.offshore_kind == "heuristic"
        assert unc.nearshore_kind == "heuristic" and unc.offshore_calibration is None
        assert "offshore_calibration" not in unc.as_dict()


def test_calibrated_offshore_component_at_a_validated_spot() -> None:
    lead = 36.0  # halfway between the 24 h and 48 h centres
    k_lo, k_hi = OFFSHORE_CALIBRATION.factors(lead)
    assert k_lo == pytest.approx((0.0858 + 0.1479) / 2)
    assert k_hi == pytest.approx((1.9384 + 1.6650) / 2)
    unc = breaking_uncertainty(6.0, lead, offshore=GFS_ONLY)
    s_off = unc.sigma_offshore

    def total(k: float) -> float:
        return math.sqrt((0.8 * k * s_off) ** 2 + (0.4 * 0.08) ** 2 + 0.30**2)

    assert unc.p10_ft == round(6.0 * math.exp(-Z80 * total(k_lo)), 1)
    assert unc.p90_ft == round(6.0 * math.exp(Z80 * total(k_hi)), 1)
    # The raw model is biased low: the range moves up (lower bound closer to the centre).
    plain = breaking_uncertainty(6.0, lead)
    assert unc.p10_ft > plain.p10_ft and unc.p90_ft > plain.p90_ft
    # Only the offshore part is calibrated; the breaking range keeps its heuristic label.
    assert unc.kind == "heuristic" and unc.nearshore_kind == "heuristic"
    assert unc.offshore_kind == "calibrated"
    d = unc.as_dict()
    assert d["offshore_calibration"] == OFFSHORE_CALIBRATION_VERSION == "ndbc-offshore-v1"
    assert d["offshore_k"] == [round(k_lo, 4), round(k_hi, 4)]
    assert d["sigma_total_lo"] < d["sigma_total"] < d["sigma_total_hi"]
    # The confidence index input (sigma_forecast) is untouched.
    assert unc.sigma_forecast == plain.sigma_forecast


@pytest.mark.parametrize(
    ("ctx", "lead", "kwargs"),
    [
        (OffshoreContext("rincon", ("ncep_gfs",), False), 24.0, {}),  # buoy: too narrow
        (OffshoreContext("pipeline", ("ncep_gfs",), False), 24.0, {}),  # buoy: too wide
        (OffshoreContext("trestles", ("ncep_gfs", "ecmwf_ifs"), False), 24.0, {}),  # blend
        (OffshoreContext("trestles", ("ecmwf_ifs",), False), 24.0, {}),  # other model
        (OffshoreContext("trestles", ("ncep_gfs",), True), 24.0, {}),  # ML-corrected Hs
        (GFS_ONLY, 250.0, {}),  # beyond the validated leads
        (GFS_ONLY, 2.0, {}),
        (GFS_ONLY, 24.0, {"ensemble": EnsembleInfo(hs_mean_m=2.0, hs_spread_m=0.2)}),
        (GFS_ONLY, 24.0, {"between_model_sigma": 0.5}),  # spread from model disagreement
    ],
)
def test_calibration_applies_only_where_it_was_validated(
    ctx: OffshoreContext, lead: float, kwargs: dict[str, object]
) -> None:
    unc = breaking_uncertainty(6.0, lead, offshore=ctx, **kwargs)  # type: ignore[arg-type]
    plain = breaking_uncertainty(6.0, lead, **kwargs)  # type: ignore[arg-type]
    assert (unc.p10_ft, unc.p90_ft, unc.offshore_calibration) == (
        plain.p10_ft,
        plain.p90_ft,
        None,
    )
    assert unc.offshore_kind != "calibrated"


def test_factors_are_constant_outside_the_centres() -> None:
    assert OFFSHORE_CALIBRATION.factors(3.0) == OFFSHORE_CALIBRATION.factors(24.0)
    assert OFFSHORE_CALIBRATION.factors(240.0) == (0.4126, 0.9488)
    assert OFFSHORE_CALIBRATION.factors(120.0) == (0.2140, 1.0678)


def test_offshore_range_matches_the_verification_scale() -> None:
    """The shipped constants give exactly the bounds the validation scored
    (verification.ranges.LeadScale with the same factors)."""
    cal = OFFSHORE_CALIBRATION
    scale = ranges.LeadScale(cal.lead_h, cal.k_lo, cal.k_hi, (0,) * len(cal.lead_h))
    leads = np.array([3.0, 18.0, 30.0, 60.0, 100.0, 200.0, 240.0])
    hs = np.array([0.6, 1.2, 2.0, 3.1, 4.4, 2.2, 1.0])
    sigma, _ = ranges.app_sigma_offshore(leads)
    k_lo, k_hi = scale.factors(leads)
    lo, hi = ranges.bounds(hs, sigma, k_lo, k_hi)
    for i, (h, lead) in enumerate(zip(hs, leads, strict=True)):
        p10, p90, kind = offshore_range(float(h), float(lead), offshore=GFS_ONLY)
        assert kind == "calibrated"
        assert p10 == pytest.approx(lo[i]) and p90 == pytest.approx(hi[i])
    p10, p90, kind = offshore_range(2.0, 24.0)
    assert kind == "heuristic"
    s = math.hypot(lead_sigma(24.0), SIGMA_FLOOR)
    assert (p10, p90) == pytest.approx((2.0 * math.exp(-Z80 * s), 2.0 * math.exp(Z80 * s)))
