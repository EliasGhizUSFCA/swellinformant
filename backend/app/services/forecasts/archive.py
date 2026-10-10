"""Prediction archive: an immutable record of every forecast issued to users.

Forecast runs (and their hourly rows) are deleted after RETENTION_FORECAST_DAYS, and the
values a model forecast at a given lead time cannot be reconstructed later. So after each
prediction job, the authoritative run of every spot is copied into ``prediction_archive``
at 3-hourly valid times up to ARCHIVE_MAX_LEAD_HOURS ahead, with the offshore inputs, our
breaking-height estimate and its uncertainty, the score and confidence, and the versions
that produced them. Rows are never updated (ON CONFLICT DO NOTHING): rescoring a run
after an algorithm upgrade does not rewrite what was issued. The archive has its own
retention (RETENTION_ARCHIVE_DAYS) and is what breaking-height verification will use once
nearshore observations exist.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import PredictionArchive, SurfQualityPrediction, WaveForecast
from app.services.forecasts.queries import latest_runs_by_spot

logger = logging.getLogger(__name__)

ARCHIVE_STEP_HOURS = 3
ARCHIVE_MAX_LEAD_HOURS = 240
INSERT_CHUNK = 2000
OFFSHORE_FIELDS = (
    "sig_wave_height_m",
    "wave_period_s",
    "wave_direction_deg",
    "primary_swell_height_m",
    "primary_swell_period_s",
    "primary_swell_direction_deg",
    "wind_speed_kmh",
    "wind_direction_deg",
)


def archive_authoritative(db: Session, now: datetime | None = None) -> dict[str, int]:
    chosen = latest_runs_by_spot(db, now)
    by_run: dict[int, list[int]] = {}
    runs = {}
    for spot_id, run in chosen.items():
        done = set(run.details.get("archived_spot_ids", []))
        if spot_id not in done:
            by_run.setdefault(run.id, []).append(spot_id)
            runs[run.id] = run
    inserted = 0
    for run_id, spot_ids in by_run.items():
        run = runs[run_id]
        rows: list[dict[str, Any]] = []
        for pred, fc in db.execute(
            select(SurfQualityPrediction, WaveForecast)
            .join(WaveForecast, WaveForecast.id == SurfQualityPrediction.forecast_id)
            .where(
                SurfQualityPrediction.run_id == run_id,
                SurfQualityPrediction.spot_id.in_(spot_ids),
            )
        ):
            lead = (pred.valid_time - run.issued_at).total_seconds() / 3600.0
            if (
                pred.valid_time.hour % ARCHIVE_STEP_HOURS
                or not -6 <= lead <= ARCHIVE_MAX_LEAD_HOURS
            ):
                continue
            rows.append(
                {
                    "spot_id": pred.spot_id,
                    "source_code": run.source.code,
                    "run_key": run.run_key,
                    "issued_at": run.issued_at,
                    "valid_time": pred.valid_time,
                    "lead_hours": round(lead, 2),
                    **{k: getattr(fc, k) for k in OFFSHORE_FIELDS},
                    "breaking_height_mid_ft": pred.breaking_height_mid_ft,
                    "breaking_p10_ft": pred.breaking_p10_ft,
                    "breaking_p90_ft": pred.breaking_p90_ft,
                    "uncertainty_kind": pred.uncertainty_kind,
                    "score": pred.score,
                    "confidence": pred.confidence,
                    "method": pred.breaking_height_method,
                    "algorithm_version": pred.algorithm_version,
                    "ml_model_version": pred.ml_model_version,
                }
            )
        # RETURNING counts the rows actually inserted (conflicts return nothing).
        stmt = (
            insert(PredictionArchive)
            .on_conflict_do_nothing(
                index_elements=["source_code", "spot_id", "issued_at", "valid_time"]
            )
            .returning(PredictionArchive.id)
        )
        for i in range(0, len(rows), INSERT_CHUNK):
            inserted += len(db.execute(stmt, rows[i : i + INSERT_CHUNK]).all())
        run.details = {
            **run.details,
            "archived_spot_ids": sorted({*run.details.get("archived_spot_ids", []), *spot_ids}),
        }
    db.commit()
    if inserted:
        logger.info("Archived %d predictions from %d runs", inserted, len(by_run))
    return {"archived": inserted, "runs": len(by_run)}
