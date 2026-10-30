"""Published forecast versions (design D7, contract C2): one row per staged refresh.

Every refresh stages a version after acquisition, scoring and validation (status
``staging``) and then publishes it in one transaction: the previous ``published`` row
becomes ``superseded`` and this one ``published`` (app.services.forecasts.versions). The
public read paths serve only the published version: ``spot_runs`` maps each spot to the
forecast run it is served from (a spot whose refresh failed keeps its previous run and is
marked ``stale``). The unique partial index ``uq_forecast_versions_published`` allows at
most one published row. ``id`` is the public version id (GET /api/forecasts/version).

* ``provider``: the FORECAST_PROVIDERS token that produced the data (``open_meteo``,
  ``windy``, ``demo``...); ``fallback_from`` the primary it replaced (``windy`` when a Windy
  refresh fell back to Open-Meteo, design D4).
* ``source_code``: the forecast_sources code of ``forecast_run_id``.
* ``wind_model`` / ``wind_model_run`` and ``wave_model`` / ``wave_model_run``: the model and
  run time of the atmosphere and wave components (a carried component keeps its original
  run time); ``*_run_estimated`` when the run time is an estimate (Windy).
* ``components``: ``{component: {model, run_key, init_time, available_at, retrieved_at,
  changed, carried_from_run_id, estimated, update_interval_seconds, domains: [{model,
  init_time, available_at}]}}`` (versions.ComponentRun.as_dict).
* ``spot_runs``: ``{"<spot_id>": {run_id, status: updated|stale|missing, last_updated_at,
  error}}`` (versions.SpotRun.as_dict).
* ``detected_at`` ... ``superseded_at``: the pipeline's timestamps (latency figures);
  ``timings``: per-stage durations.
* ``error_message``: why a version failed (redacted; never served by the public API).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

VERSION_STATUSES = ("staging", "published", "superseded", "failed")
SPOT_RUN_STATUSES = ("updated", "stale", "missing")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class ForecastVersion(Base):
    __tablename__ = "forecast_versions"
    __table_args__ = (
        # At most one current version.
        Index(
            "uq_forecast_versions_published",
            "status",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
        Index("ix_forecast_versions_published_at", text("published_at DESC")),
        CheckConstraint(_in("status", VERSION_STATUSES), name="status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="staging")
    provider: Mapped[str | None] = mapped_column(String(40))
    source_code: Mapped[str | None] = mapped_column(String(40))
    forecast_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("forecast_runs.id", ondelete="SET NULL")
    )
    previous_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("forecast_versions.id", ondelete="SET NULL")
    )
    sync_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sync_runs.id", ondelete="SET NULL")
    )
    wind_model: Mapped[str | None] = mapped_column(String(60))
    wind_model_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wave_model: Mapped[str | None] = mapped_column(String(60))
    wave_model_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wind_run_estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    wave_run_estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    fallback_from: Mapped[str | None] = mapped_column(String(40))
    components: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    spot_runs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    spots_total: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    spots_updated: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    spots_stale: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    spots_failed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    upstream_available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    staged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    timings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    error_message: Mapped[str | None] = mapped_column(Text)
