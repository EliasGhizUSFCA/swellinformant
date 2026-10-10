"""Delivery records for account emails (verification, password reset, test messages).

Alert emails live in ``notifications``. Account emails get the same lifecycle here so that
"we sent you a link" is backed by a record of what actually happened: queued → sending →
accepted (provider took it) / captured (development inbox) → delivered / bounced via
provider webhooks, or failed with the provider's reason. Message bodies are not stored:
they contain single-use tokens.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.enums import EmailDeliveryStatus, EmailPurpose
from app.models.types import TimestampMixin, str_enum


class EmailMessage(TimestampMixin, Base):
    __tablename__ = "email_messages"
    __table_args__ = (
        Index("ix_email_messages_user_created", "user_id", "created_at"),
        Index("ix_email_messages_provider_message", "provider", "provider_message_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[EmailPurpose] = mapped_column(
        str_enum(EmailPurpose, "email_purpose"), nullable=False
    )
    status: Mapped[EmailDeliveryStatus] = mapped_column(
        str_enum(EmailDeliveryStatus, "email_delivery_status"), nullable=False
    )
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(20))
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Provider webhook events as received (event type + timestamp), newest last.
    events: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
