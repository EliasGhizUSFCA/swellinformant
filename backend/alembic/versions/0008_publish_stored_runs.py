"""publish the data stored before syncs existed (data only)

A database created before Upgrade 4 has forecast runs, predictions and map fields but no
sync_runs row (0007 starts the table empty), so nothing counts as published:

* the status page and the map index name no publication (/api/health/data
  ``last_published`` and /api/map/fields ``published_at`` are null);
* the sync's known runs (app.services.sync.detect.load_known: "before the first publication
  nothing counts as stored") are empty, so the first check after the upgrade finds every
  component new and runs a whole sync over data that is already stored and served.

This migration records one publication of what is stored, as a sync's publish stage would
have: one sync_runs row with trigger ``pipeline`` (the pre-sync pipeline stored the data)
and status ``success``, published at the completion of the newest usable stored run (so
every stored run counts as published and none as new; the time is when that data became
servable, not the time of the upgrade), with ``map_run_id`` the best servable map run
(app.services.map_fields.servable_runs_query, mirrored below) and ``blend_run_id`` the
newest blend run with predictions (app.services.sync.publish.newest_blend_run_id).

Nothing happens when a publication exists already or no usable run is stored (a new
database: its first sync publishes). Schema unchanged. Downgrade deletes only the row this
migration wrote (plan.published_by).

Revision ID: 0008_publish_stored_runs
Revises: 0007_sync_runs
Create Date: 2026-10-23 19:00:00
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import bindparam, text

revision: str = "0008_publish_stored_runs"
down_revision: str | Sequence[str] | None = "0007_sync_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PUBLISHED_BY = "migration 0008_publish_stored_runs"
# Frozen copies (a migration must not follow later code): the run statuses that are
# servable (app.models.enums.ForecastRunStatus), the blend's source code, and the six core
# map variables a complete valid time has (app.models.forecast_engine.CORE_MAP_FIELD_VARIABLES).
USABLE = ("success", "partial")
BLEND_CODE = "blend"
CORE_VARIABLES = ("hs", "tp", "wave_dir", "wind_u", "wind_v", "pressure")

_PUBLISHED_EXISTS = text("SELECT EXISTS (SELECT 1 FROM sync_runs WHERE published_at IS NOT NULL)")
_NEWEST_COMPLETION = text(
    "SELECT max(completed_at) FROM forecast_runs WHERE status IN :usable"
).bindparams(bindparam("usable", USABLE, expanding=True))
# map_fields.servable_runs_query(limit=1): runs with at least one valid time holding every
# core variable; the newest model cycle first, then the most complete valid times, then the
# newer run.
_BEST_MAP_RUN = text(
    """
    WITH per_time AS (
        SELECT run_id,
               max(cycle_time) AS cycle,
               count(DISTINCT variable) FILTER (WHERE variable IN :core) AS n
        FROM map_fields
        GROUP BY run_id, valid_time
    )
    SELECT run_id
    FROM per_time
    GROUP BY run_id
    HAVING count(*) FILTER (WHERE n = :n_core) > 0
    ORDER BY max(cycle) DESC, count(*) FILTER (WHERE n = :n_core) DESC, run_id DESC
    LIMIT 1
    """
).bindparams(bindparam("core", CORE_VARIABLES, expanding=True), n_core=len(CORE_VARIABLES))
_NEWEST_BLEND = text(
    """
    SELECT r.id
    FROM forecast_runs r JOIN forecast_sources s ON s.id = r.source_id
    WHERE s.code = :blend AND r.status IN :usable AND r.predictions_computed_at IS NOT NULL
    ORDER BY r.issued_at DESC, r.id DESC
    LIMIT 1
    """
).bindparams(bindparam("usable", USABLE, expanding=True), blend=BLEND_CODE)


def upgrade() -> None:
    """Upgrade data: one publication of the runs stored before the first sync."""
    conn = op.get_bind()
    if conn.scalar(_PUBLISHED_EXISTS):
        return
    published_at = conn.scalar(_NEWEST_COMPLETION)
    if published_at is None:
        return
    conn.execute(
        text(
            """
            INSERT INTO sync_runs (trigger, status, started_at, finished_at, plan, stages,
                                   published_at, map_run_id, blend_run_id)
            VALUES ('pipeline', 'success', :at, :at,
                    jsonb_build_object('published_by', CAST(:published_by AS text)),
                    '[]'::jsonb, :at, :map_run_id, :blend_run_id)
            """
        ),
        {
            "at": published_at,
            "published_by": PUBLISHED_BY,
            "map_run_id": conn.scalar(_BEST_MAP_RUN),
            "blend_run_id": conn.scalar(_NEWEST_BLEND),
        },
    )


def downgrade() -> None:
    """Downgrade data: delete the publication this migration recorded."""
    op.get_bind().execute(
        text("DELETE FROM sync_runs WHERE plan ->> 'published_by' = :published_by"),
        {"published_by": PUBLISHED_BY},
    )
