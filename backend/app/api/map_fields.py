"""Map ocean layers: the field index and the field bytes (public, cacheable).

Contract: app/schemas/map_fields.py. The implementation lives in
app/services/map_fields.py (``field_index`` and ``field_bytes``). A field URL with ``run``
names one model run, so its bytes never change and are cached for an hour; without it the
current run is served, which a new cycle replaces, so it is cached for five minutes.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy.orm import Session

from app.core.database import get_db, utcnow
from app.core.errors import NotFound
from app.models.forecast_engine import MAP_FIELD_VARIABLES
from app.schemas.map_fields import FieldIndexOut
from app.services import map_fields

router = APIRouter(prefix="/api/map", tags=["map"])

VARIABLE_PATTERN = "^(" + "|".join(MAP_FIELD_VARIABLES) + ")$"


@router.get("/fields", response_model=FieldIndexOut)
def fields_index(response: Response, db: Session = Depends(get_db)) -> FieldIndexOut:
    """Which global fields exist for the current model run, and their valid times."""
    response.headers["Cache-Control"] = "public, max-age=300"
    return map_fields.field_index(db, utcnow())


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
            "(run not retained); default the current run."
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
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={
            "X-Field-Meta": meta.model_dump_json(),
            "Cache-Control": f"public, max-age={3600 if run else 300}",
            "Access-Control-Expose-Headers": "X-Field-Meta",
        },
    )
