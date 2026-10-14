"""Tide stage at given times, from the stored predictions (used by the quality score).

Not implemented yet: :func:`tide_stages_for` returns no stages, so the tide stays unscored.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class TideStage:
    # 0 at the preceding low water, 1 at the next high water (and back to 0 at the next low).
    stage: float
    rising: bool
    # Height difference between the surrounding low and high water (metres).
    range_m: float
    # What produced the predictions, e.g. "noaa_coops" or "eot20".
    source: str


def tide_stages_for(
    db: Session, spot_id: int, times: Sequence[datetime]
) -> dict[datetime, TideStage]:
    """The stage at each of ``times`` that a prediction covers; other times are absent."""
    return {}
