"""research source retrieval

* spot_research_sources.retrieval: how a source was read, "fetched" (the page or
  publication itself) or "web_search" (only a search tool's summary of the page, so the
  excerpt is the search tool's text about the page). Existing rows were recorded as read
  directly and get "fetched"; the research-v1.1 methodology caps a variable supported only
  by "web_search" sources at Moderate.

No data is changed or removed; downgrade drops the column and its check constraint.

Revision ID: 0005_research_retrieval
Revises: 0004_confidence_map_fields
Create Date: 2026-10-12 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_research_retrieval"
down_revision: str | Sequence[str] | None = "0004_confidence_map_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "spot_research_sources",
        sa.Column(
            "retrieval",
            sa.String(length=16),
            server_default="fetched",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_spot_research_sources_retrieval"),
        "spot_research_sources",
        "retrieval IN ('fetched', 'web_search')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        op.f("ck_spot_research_sources_retrieval"), "spot_research_sources", type_="check"
    )
    op.drop_column("spot_research_sources", "retrieval")
