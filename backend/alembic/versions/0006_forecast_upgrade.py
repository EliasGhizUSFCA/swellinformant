"""forecast upgrade: map-field partitions, tides and spot wind profiles

* map_fields.variable accepts the nine wave-partition variables (swell1_*, swell2_*,
  windsea_* for height, period and direction) next to the six core ones;
* tide_stations / tide_predictions / spot_tide_stations: predicted tide heights per station
  and datum, high and low waters, and the station(s) that represent each spot;
* spot_physical_profiles.wind_profile: optional researched wind behaviour per spot.

Existing rows are kept: the new column is nullable and the new tables start empty.
Downgrade deletes the partition map-field rows (the old CHECK constraint does not allow
them), then drops the new tables and the column.

Revision ID: 0006_forecast_upgrade
Revises: 0005_research_retrieval
Create Date: 2026-10-14 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_forecast_upgrade"
down_revision: str | Sequence[str] | None = "0005_research_retrieval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CORE_VARIABLES = ("hs", "tp", "wave_dir", "wind_u", "wind_v", "pressure")
PARTITION_VARIABLES = (
    "swell1_hs",
    "swell1_tp",
    "swell1_dir",
    "swell2_hs",
    "swell2_tp",
    "swell2_dir",
    "windsea_hs",
    "windsea_tp",
    "windsea_dir",
)


def _variable_check(names: Sequence[str]) -> str:
    return "variable IN (" + ", ".join(f"'{v}'" for v in names) + ")"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(op.f("ck_map_fields_variable"), "map_fields", type_="check")
    op.create_check_constraint(
        op.f("ck_map_fields_variable"),
        "map_fields",
        _variable_check(CORE_VARIABLES + PARTITION_VARIABLES),
    )

    op.create_table(
        "tide_stations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=True),
        sa.Column("reference_code", sa.String(length=32), nullable=True),
        sa.Column(
            "datums",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("licence", sa.String(length=64), nullable=True),
        sa.Column("attribution", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
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
        sa.CheckConstraint(
            "kind IN ('harmonic', 'subordinate', 'model')", name=op.f("ck_tide_stations_kind")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tide_stations")),
        sa.UniqueConstraint("provider", "code", name=op.f("uq_tide_stations_provider_code")),
    )
    op.create_table(
        "tide_predictions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("station_id", sa.Integer(), nullable=False),
        sa.Column("valid_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("height_m", sa.Float(), nullable=False),
        sa.Column("datum", sa.String(length=16), nullable=False),
        sa.Column("is_event", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("event_type", sa.String(length=4), nullable=True),
        sa.Column("interpolated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("source_code", sa.String(length=32), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('high', 'low')", name=op.f("ck_tide_predictions_event_type")
        ),
        sa.ForeignKeyConstraint(
            ["station_id"],
            ["tide_stations.id"],
            name=op.f("fk_tide_predictions_station_id_tide_stations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tide_predictions")),
        sa.UniqueConstraint(
            "station_id",
            "valid_time",
            "datum",
            "is_event",
            name=op.f("uq_tide_predictions_station_id_valid_time_datum_is_event"),
        ),
    )
    op.create_index(
        "ix_tide_predictions_station_valid",
        "tide_predictions",
        ["station_id", "valid_time"],
        unique=False,
    )
    op.create_table(
        "spot_tide_stations",
        sa.Column("spot_id", sa.Integer(), nullable=False),
        sa.Column("station_id", sa.Integer(), nullable=False),
        sa.Column("distance_km", sa.Float(), nullable=False),
        sa.Column("relation", sa.String(length=16), nullable=True),
        sa.Column("justification", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "relation IN ('primary', 'fallback')", name=op.f("ck_spot_tide_stations_relation")
        ),
        sa.ForeignKeyConstraint(
            ["spot_id"],
            ["surf_spots.id"],
            name=op.f("fk_spot_tide_stations_spot_id_surf_spots"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["station_id"],
            ["tide_stations.id"],
            name=op.f("fk_spot_tide_stations_station_id_tide_stations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("spot_id", "station_id", name=op.f("pk_spot_tide_stations")),
    )

    op.add_column(
        "spot_physical_profiles",
        sa.Column("wind_profile", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("spot_physical_profiles", "wind_profile")
    op.drop_table("spot_tide_stations")
    op.drop_index("ix_tide_predictions_station_valid", table_name="tide_predictions")
    op.drop_table("tide_predictions")
    op.drop_table("tide_stations")

    # The six-variable CHECK cannot hold partition rows: remove them first.
    op.execute(
        sa.text("DELETE FROM map_fields WHERE variable IN :names").bindparams(
            sa.bindparam("names", value=list(PARTITION_VARIABLES), expanding=True)
        )
    )
    op.drop_constraint(op.f("ck_map_fields_variable"), "map_fields", type_="check")
    op.create_check_constraint(
        op.f("ck_map_fields_variable"), "map_fields", _variable_check(CORE_VARIABLES)
    )
