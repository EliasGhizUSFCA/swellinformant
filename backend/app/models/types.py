"""Column helpers used across models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import utcnow


def str_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """Non-native enum stored as VARCHAR with a named CHECK constraint."""
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        length=32,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )
