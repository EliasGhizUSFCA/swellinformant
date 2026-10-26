"""Tide predictions and nearby observations for one spot.

``GET /api/spots/{slug}/tides?start&end`` returns a :class:`TideBlockOut` and
``GET /api/spots/{slug}/observations`` an :class:`ObservationsBlockOut`; the timeline
(app/schemas/timeline.py) embeds both.

Tides are predictions (the astronomical tide at a station or model point), never observed
water levels, and every height is relative to the stated ``datum``. Observations are
measurements (or reports) made at stations near the spot, kept apart from any forecast.
When nothing is configured or available, the blocks say so (status "unavailable" and a
reason) instead of carrying made-up values.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.spots import AttributionLinkOut

TideStatus = Literal["available", "unavailable", "demo"]
TideDatum = Literal["MLLW", "MSL", "LAT", "model_msl"]
TideStationKind = Literal["harmonic", "subordinate", "model"]
ObservationsStatus = Literal["available", "unavailable", "stale"]
ObservationKind = Literal["instrument", "human_report", "model_analysis"]

# Shown when the spot's tide preference is unknown, weakly researched or disputed.
TIDE_PREFERENCE_NOT_ESTABLISHED = "Tide preference not well established."


class TideStationOut(BaseModel):
    code: str
    name: str
    provider: str
    kind: TideStationKind
    # Great-circle distance from the spot.
    distance_km: float


class TidePointOut(BaseModel):
    t: datetime
    height_m: float
    # True for points computed between a subordinate station's high and low waters.
    interpolated: bool


class TideEventOut(BaseModel):
    t: datetime
    kind: Literal["high", "low"]
    height_m: float


class TidePreferenceOut(BaseModel):
    """The spot's researched tide preference, and how far it can be trusted."""

    value: str | None  # e.g. "mid", "low to mid"; None = not researched
    research_class: str | None  # tide research confidence: "high" | "moderate" | "low"
    decision: str | None  # the profile decision for the tide variable, e.g. "disputed"
    # Plain text for the UI; TIDE_PREFERENCE_NOT_ESTABLISHED when the class is low, the
    # decision is disputed or the value is None.
    text: str


class TideBlockOut(BaseModel):
    status: TideStatus
    reason: str | None = None
    station: TideStationOut | None = None
    # What produced the predictions, e.g. "noaa_coops" or "eot20".
    source: str | None = None
    datum: TideDatum | None = None
    units: Literal["m"] = "m"
    # Heights at 30-60 minute spacing.
    curve: list[TidePointOut] = []
    events: list[TideEventOut] = []
    preference: TidePreferenceOut
    attribution: str | None = None
    # Licence and source links the attribution needs (EOT20: CC BY 4.0 and the dataset DOI;
    # CO-OPS: tidesandcurrents.noaa.gov), shown with the attribution.
    attribution_links: list[AttributionLinkOut] = []
    fetched_at: datetime | None = None


class ObservationOut(BaseModel):
    t: datetime
    kind: ObservationKind
    station_code: str
    station_name: str
    provider: str
    # How the station relates to the spot (offshore_reference or nearshore_reference).
    relation: str
    distance_km: float
    hs_m: float | None
    hs_kind: str | None  # "spectral_hm0" | "h_third" | "unknown"
    tp_s: float | None
    dir_from_deg: float | None
    wind_kmh: float | None
    qc_flag: str | None
    stale: bool
    licence: str | None
    attribution: str | None
    # The provider in words ("NOAA NDBC", "IMOS / AODN") and the licence link, when known.
    provider_name: str | None = None
    licence_url: str | None = None


class ObservationsBlockOut(BaseModel):
    status: ObservationsStatus
    reason: str | None = None
    items: list[ObservationOut] = []
