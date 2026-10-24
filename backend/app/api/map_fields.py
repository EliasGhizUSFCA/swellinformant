"""Map ocean layers: the field index and the field bytes (public, cacheable).

Contract: app/schemas/map_fields.py. The implementation lives in
app/services/map_fields.py (``field_index`` and ``field_bytes``). Neither depends on the
visitor, so both carry shared-cache headers (app.core.http_cache, docs/map-layers.md):

* the index: the windows of every public GET (app.api.spots: browsers 60 s, the CDN 300 s
  plus up to an hour of stale-while-revalidate). It names the served run, which a
  publication switches about every 6 hours, and switches at the edge together with the
  spot list and the map conditions it is shown with; like them it comes from the public
  snapshot cache (computed for the start of the UTC hour).
* a field whose URL names the run (``run``) and the exact valid time it serves (``at``):
  that URL's bytes never change, so browsers keep it an hour and the CDN a year
  (``immutable``). The client always asks this way (lib/map-fields.ts ``fieldUrl``).
* any other field URL (no ``run``, no ``at``, or an ``at`` between valid times): the
  answer can change with the served run or the clock, so 5 minutes for both.

Errors (404, 422) set no cache header, so the security middleware's ``no-store`` applies.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from pydantic import TypeAdapter
from sqlalchemy.orm import Session

from app.api.spots import public_json
from app.core.database import get_db
from app.core.errors import NotFound
from app.core.http_cache import cache_headers
from app.core.ratelimit import client_ip
from app.models.forecast_engine import MAP_FIELD_VARIABLES
from app.schemas.map_fields import FieldIndexOut
from app.services import map_fields
from app.services import public_cache as snapshots

router = APIRouter(prefix="/api/map", tags=["map"])

VARIABLE_PATTERN = "^(" + "|".join(MAP_FIELD_VARIABLES) + ")$"
RUN_FIELD_BROWSER_SECONDS = 3600
RUN_FIELD_CDN_SECONDS = 31_536_000  # one year: the URL names the run and the valid time
FIELD_SECONDS = 300


FIELD_INDEX = TypeAdapter(FieldIndexOut)


@router.get("/fields", response_model=FieldIndexOut)
def fields_index(request: Request, db: Session = Depends(get_db)) -> Response:
    """Which global fields exist for the served model run, and their valid times."""
    # From the public snapshot cache (app.services.public_cache), like the spot list and the
    # conditions it is shown with: computed at most once per publication and UTC hour, for
    # the start of the hour (its age and staleness are judged then), so a map page whose
    # CDN copy expired every 5 minutes no longer wakes the database each time.
    return public_json(
        snapshots.snapshot(
            db,
            "map-field-index",
            "",
            lambda now: snapshots.render(FIELD_INDEX, map_fields.field_index(db, now)),
            visitor=client_ip(request),
        )
    )


@router.get(
    "/fields/{variable}",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
def field_data(
    variable: str = Path(..., pattern=VARIABLE_PATTERN),
    at: datetime | None = Query(None, description="Valid time (ISO 8601); default now"),
    run: str | None = Query(
        None,
        max_length=120,
        description=(
            "Model run (the index's run_key). Served while it is retained, otherwise 404 "
            "(run not retained); default the served run."
        ),
    ),
    db: Session = Depends(get_db),
) -> Response:
    """One field as zlib-compressed uint8 bytes; decoding parameters in X-Field-Meta."""
    try:
        found = map_fields.field_bytes(db, variable, at, run)
    except map_fields.RunNotRetained as exc:
        raise NotFound(
            "Map field run not retained: re-read /api/map/fields for the current run.",
            code="run_not_retained",
        ) from exc
    if found is None:
        raise NotFound("No map field for that variable and time.")
    meta, payload = found
    response = Response(
        content=payload,
        media_type="application/octet-stream",
        headers={
            "X-Field-Meta": meta.model_dump_json(),
            "Access-Control-Expose-Headers": "X-Field-Meta",
        },
    )
    if run is not None and at is not None and map_fields.same_instant(at, meta.valid_time):
        cache_headers(
            response,
            browser_seconds=RUN_FIELD_BROWSER_SECONDS,
            cdn_seconds=RUN_FIELD_CDN_SECONDS,
            immutable=True,
        )
    else:
        cache_headers(response, browser_seconds=FIELD_SECONDS, cdn_seconds=FIELD_SECONDS)
    return response
