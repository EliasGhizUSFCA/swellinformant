"""The tide and observation blocks for one spot (contract: app/schemas/tides.py).

Not implemented yet: both blocks report "unavailable" with a reason, so the API and the
timeline already carry the final shape without any made-up value.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import SurfSpot
from app.schemas.tides import (
    TIDE_PREFERENCE_NOT_ESTABLISHED,
    ObservationsBlockOut,
    TideBlockOut,
    TidePreferenceOut,
)

NO_TIDE_SOURCE = "No tide source configured for this spot."
NO_OBSERVATIONS = "Observations are not available for this spot yet."


def spot_tide_block(
    db: Session, spot: SurfSpot, start: datetime, end: datetime, now: datetime | None = None
) -> TideBlockOut:
    """Predicted tide curve and high/low waters at the spot's station for [start, end)."""
    preference = spot.tide_preference.value if spot.tide_preference is not None else None
    return TideBlockOut(
        status="unavailable",
        reason=NO_TIDE_SOURCE,
        preference=TidePreferenceOut(
            value=preference,
            research_class=None,
            decision=None,
            text=TIDE_PREFERENCE_NOT_ESTABLISHED,
        ),
    )


def spot_observations_block(
    db: Session, spot: SurfSpot, start: datetime, end: datetime, now: datetime | None = None
) -> ObservationsBlockOut:
    """Measured conditions at the spot's offshore and nearshore reference stations."""
    return ObservationsBlockOut(status="unavailable", reason=NO_OBSERVATIONS)
