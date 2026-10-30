"""Map ocean layers: which global fields exist and how to decode them.

The field bytes themselves are served by ``GET /api/map/fields/{variable}?at=ISO`` as
``application/octet-stream``: zlib (RFC 1950, "deflate" in the browser's
DecompressionStream) compressed uint8, ``nx * ny`` cells, row-major, row 0 at latitude
``lat0`` (northernmost), row j at ``lat0 - j * dlat``, column i at ``lon0 + i * dlon``
(``lon0`` = -180, wrapping east). value = ``offset + scale * byte``; byte 255 = no data
(land, ice, missing). The decoding parameters are repeated in the ``X-Field-Meta``
response header (JSON with the same keys as :class:`FieldMeta`).

Variables (models.forecast_engine): the six core ones (hs, tp, wave_dir, wind_u, wind_v,
pressure) and, when a run stores them, nine partition ones: swell1_* (primary swell),
swell2_* (secondary swell) and windsea_* (wind sea), each with _hs (m, encoded like hs),
_tp (s, like tp) and _dir (degrees FROM, cyclic like wave_dir; no +180). An absent
partition is byte 255 (no data), never 0.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

# "core": the six variables every servable valid time has; "partition": optional layers.
FieldGroup = Literal["core", "partition"]
WaveSystem = Literal["primary", "secondary", "windsea"]


class FieldMeta(BaseModel):
    variable: str  # a core or partition variable name (see the module docstring)
    group: FieldGroup = "core"
    unit: str
    valid_time: datetime
    run_key: str
    nx: int
    ny: int
    lat0: float
    lon0: float
    dlat: float
    dlon: float
    scale: float
    offset: float
    nodata: int = 255


class FieldVariableOut(BaseModel):
    name: str
    label: str
    unit: str
    # Display range (values beyond it saturate the colour scale).
    display_min: float
    display_max: float
    group: FieldGroup = "core"
    # Partition variables: which wave system they describe.
    system: WaveSystem | None = None


class FieldSourceOut(BaseModel):
    code: str
    name: str
    attribution: str
    is_demo: bool


class FieldIndexOut(BaseModel):
    available: bool
    # False when the site switched the ocean layers off on purpose (MAP_FIELDS_ENABLED=false,
    # the daemon-less setting) and no fields are stored: the map then offers no layers and
    # shows no outage notice. True otherwise, also while fields are missing or late.
    enabled: bool = True
    # Why no fields are available (e.g. live NOAA data not enabled), in plain language.
    message: str | None = None
    source: FieldSourceOut | None = None
    run_key: str | None = None
    cycle_time: datetime | None = None
    valid_times: list[datetime] = []
    variables: list[FieldVariableOut] = []
    # Freshness: when the served run was completed, the age of its model cycle (hours), the
    # limit after which it counts as stale (MAP_FIELD_STALE_AFTER_HOURS) and the verdict.
    updated_at: datetime | None = None
    age_hours: float | None = None
    stale_after_hours: float = 12.0
    stale: bool = False
    # The sync publication the served run belongs to (SYNC_PUBLISH_GATE): when it was
    # published and its sync_runs id. None when no sync has been published.
    published_at: datetime | None = None
    sync_run_id: int | None = None
