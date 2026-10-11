"""email delivery tracking

Account emails get delivery records (email_messages); alerts gain provider delivery
confirmation (delivered_at, events) and honest statuses: "sent" becomes "accepted" (the
provider took the message) and new states capture dev inboxes, webhook confirmations and
suppressed alerts.

Revision ID: 0002_email_delivery
Revises: 0001_initial
Create Date: 2026-10-10 21:44:25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_email_delivery"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = ("queued", "sending", "sent", "failed", "skipped")
NEW_STATUSES = (
    "queued",
    "sending",
    "accepted",
    "captured",
    "delivered",
    "bounced",
    "complained",
    "rejected",
    "failed",
    "skipped",
    "suppressed",
)


def _status_check(values: Sequence[str]) -> str:
    return "status IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.create_table(
        "email_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "purpose",
            sa.Enum(
                "verify_email",
                "password_reset",
                "test",
                name="email_purpose",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "sending",
                "accepted",
                "captured",
                "delivered",
                "bounced",
                "complained",
                "rejected",
                "failed",
                name="email_delivery_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "events",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_email_messages_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_messages")),
    )
    op.create_index(
        "ix_email_messages_provider_message", "email_messages", ["provider", "provider_message_id"]
    )
    op.create_index("ix_email_messages_user_created", "email_messages", ["user_id", "created_at"])

    op.add_column("notifications", sa.Column("delivered_at", sa.DateTime(timezone=True)))
    op.add_column(
        "notifications",
        sa.Column(
            "events",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.drop_constraint(op.f("ck_notifications_notification_status"), "notifications", type_="check")
    # "sent" only ever meant "the provider accepted it" (or a dev outbox captured it).
    op.execute(
        "UPDATE notifications SET status = CASE WHEN provider = 'console' "
        "THEN 'captured' ELSE 'accepted' END WHERE status = 'sent'"
    )
    op.create_check_constraint(
        op.f("ck_notifications_notification_status"), "notifications", _status_check(NEW_STATUSES)
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_notifications_notification_status"), "notifications", type_="check")
    op.execute(
        "UPDATE notifications SET status = 'sent' "
        "WHERE status IN ('accepted', 'captured', 'delivered', 'bounced', 'complained')"
    )
    op.execute("UPDATE notifications SET status = 'failed' WHERE status = 'rejected'")
    op.execute("UPDATE notifications SET status = 'skipped' WHERE status = 'suppressed'")
    op.create_check_constraint(
        op.f("ck_notifications_notification_status"), "notifications", _status_check(OLD_STATUSES)
    )
    op.drop_column("notifications", "events")
    op.drop_column("notifications", "delivered_at")
    op.drop_index("ix_email_messages_user_created", table_name="email_messages")
    op.drop_index("ix_email_messages_provider_message", table_name="email_messages")
    op.drop_table("email_messages")
