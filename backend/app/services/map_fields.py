"""Map ocean layers (read side). STUB: the field pipeline is not implemented yet.

``field_index`` reports that no fields exist and why; ``field_bytes`` finds nothing. The
map-fields work replaces both with real implementations over the ``map_fields`` table
(see app/schemas/map_fields.py for the contract and models.forecast_engine.MapField for
the storage format).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.schemas.map_fields import FieldIndexOut, FieldMeta


def field_index(db: Session) -> FieldIndexOut:
    return FieldIndexOut(
        available=False,
        message="Ocean layers are not available yet.",
    )


def field_bytes(db: Session, variable: str, at: datetime | None) -> tuple[FieldMeta, bytes] | None:
    return None
