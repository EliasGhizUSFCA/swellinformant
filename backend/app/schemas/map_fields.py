"""Map ocean layers: which global fields exist and how to decode them.

The field bytes themselves are served by ``GET /api/map/fields/{variable}?at=ISO`` as
``application/octet-stream``: zlib (RFC 1950, "deflate" in the browser's
DecompressionStream) compressed uint8, ``nx * ny`` cells, row-major, row 0 at latitude
``lat0`` (northernmost), row j at ``lat0 - j * dlat``, column i at ``lon0 + i * dlon``
(``lon0`` = -180, wrapping east). value = ``offset + scale * byte``; byte 255 = no data
(land, ice, missing). The decoding parameters are repeated in the ``X-Field-Meta``
response header (JSON with the same keys as :class:`FieldMeta`).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class FieldMeta(BaseModel):
    variable: str  # hs | tp | wave_dir | wind_u | wind_v | pressure
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


class FieldSourceOut(BaseModel):
    code: str
    name: str
    attribution: str
    is_demo: bool


class FieldIndexOut(BaseModel):
    available: bool
    # Why no fields are available (e.g. live NOAA data not enabled), in plain language.
    message: str | None = None
    source: FieldSourceOut | None = None
    run_key: str | None = None
    cycle_time: datetime | None = None
    valid_times: list[datetime] = []
    variables: list[FieldVariableOut] = []
