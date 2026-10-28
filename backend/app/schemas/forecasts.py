"""Forecast versions on the public API (design D7, D10, D11; contract C4).

* ``GET /api/forecasts/version``: :class:`ForecastVersionOut`, the small payload browsers poll
  to learn that a new forecast was published (``version_id`` null: nothing published yet,
  the data shown then comes from the run selection used before versions existed).
* ``POST /api/forecasts/seen``: :class:`ForecastSeenIn`, sent once per version per tab after
  the new data rendered (publication-to-browser latency, admin dashboard).
* ``GET /api/forecasts/status``: :class:`ForecastStatusOut`, the public freshness summary.
* :class:`SpotFreshnessOut`: the ``freshness`` of a spot on the public spot payloads (spot
  list and detail, forecast, timeline, map conditions).

Times are UTC. A model run time marked ``estimated`` is an estimate (Windy publishes no run
time), and is shown as such.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# Why a spot is stale (app.services.forecasts.versions.SpotFreshness): no data at all, its
# latest refresh failed while other spots updated, a component (wind = "atmosphere", waves =
# "wave") is older than its model's update interval plus the threshold, or (no component
# information) the spot's own last update is too old.
FreshnessReason = Literal["missing", "refresh_failed", "atmosphere_age", "wave_age", "age"]
ComponentName = Literal["atmosphere", "wave"]


class ForecastVersionOut(BaseModel):
    """The published forecast version (C4). Every field is always present."""

    version_id: int | None
    published_at: datetime | None
    provider: str | None
    source_code: str | None
    wind_model: str | None
    wind_model_run: datetime | None
    wind_run_estimated: bool
    wave_model: str | None
    wave_model_run: datetime | None
    wave_run_estimated: bool
    spots_total: int
    spots_updated: int
    spots_stale: int
    # The published map-layer run (its own pointer, sync_runs.map_run_id): a change tells the
    # client to refresh the map layers too.
    map_run_id: int | None
    map_cycle: datetime | None
    # Plain-text credits of the provider whose data the version serves.
    attribution: list[str]


class ForecastSeenIn(BaseModel):
    """A browser rendered this version (sent with navigator.sendBeacon)."""

    version_id: int = Field(ge=1)


class SpotFreshnessOut(BaseModel):
    """How fresh one spot's served forecast is (design D10).

    ``version_id``: the published version the spot is served from (None before the first
    publication: then the freshness of the run the legacy selection serves, ``stale`` by
    that run's own staleness rule). ``last_updated_at``: the spot's last successful update
    ("Forecast last updated 7 hours ago"). ``stale`` data is never labelled current.
    """

    version_id: int | None = None
    last_updated_at: datetime | None = None
    stale: bool = False
    reason: FreshnessReason | None = None


class ForecastDomainOut(BaseModel):
    """One model domain behind a component (e.g. GFS 0.13 and GFS 0.25)."""

    model: str
    model_run: datetime | None = None  # the domain's initialisation time
    available_at: datetime | None = None


class ForecastComponentOut(BaseModel):
    """The model run one component of the published version comes from."""

    component: ComponentName
    model: str | None = None
    run_key: str | None = None
    model_run: datetime | None = None  # initialisation time (the oldest domain's)
    estimated: bool = False
    available_at: datetime | None = None
    retrieved_at: datetime | None = None
    # False: this component was carried forward from an earlier refresh with its original
    # model run and retrieval time (only the other component was new).
    changed: bool = True
    domains: list[ForecastDomainOut] = []


class StaleSpotOut(BaseModel):
    spot_id: int
    slug: str
    name: str
    last_updated_at: datetime | None = None
    reason: FreshnessReason | None = None


class ForecastStatusOut(BaseModel):
    """The public freshness summary: the published version, the model run behind each
    component, and the active spots whose forecast is stale."""

    version: ForecastVersionOut
    components: list[ForecastComponentOut] = []
    spots_served: int = 0
    stale_spots: list[StaleSpotOut] = []
    # The instant the summary was computed for (the start of the UTC hour).
    generated_at: datetime
