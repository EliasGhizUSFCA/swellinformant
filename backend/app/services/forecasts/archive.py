"""Prediction archive: an immutable record of every forecast issued to users.

Forecast runs (and their hourly rows) are deleted after RETENTION_FORECAST_DAYS, and the
values a model forecast at a given lead time cannot be reconstructed later. So after each
prediction job, the authoritative run of every spot is copied into ``prediction_archive``
at 3-hourly valid times up to ARCHIVE_MAX_LEAD_HOURS ahead, with the offshore inputs, our
breaking-height estimate and its uncertainty, the score and confidence, and the versions
that produced them. Both confidence assessments are archived as issued: the forecast and
local confidence classes, the overall research class local confidence used, the
methodology and every input and rule (``confidence_inputs``), so they can be evaluated
against observations later. Rows are never updated (ON CONFLICT DO NOTHING): rescoring a run
after an algorithm upgrade does not rewrite what was issued. The archive has its own
retention (RETENTION_ARCHIVE_DAYS) and is what breaking-height verification will use once
nearshore observations exist.

With ARCHIVE_CONFIDENCE_DIGEST (default on) a row keeps a digest of the confidence inputs
instead of all of them (:func:`confidence_digest`): the rules that capped or lowered the
class and the numeric inputs those rules are thresholds on, which is what evaluating or
re-calibrating the rules against observations needs. Dropped are the explanatory notes
and the reason sentences (they follow from the rules and the method version), and the
per-variable research classes (they follow from the research assessment id). Measured on
the 3,950 archive rows of the real 2026-10-11 blend (``swell_live2``): the inputs take
1,222 B of a 1,506 B row, the digest 214 B, so a row is 499 B (docs/operations.md).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
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


DIGEST_VERSION = 1


def confidence_digest(inputs: dict[str, Any] | None) -> dict[str, Any] | None:
    """The archived digest of a prediction's ``confidence_inputs`` (see the module docstring).

    ``caps``: every rule except ``note:`` ones (``cap:`` dropped from the name, e.g.
    ``low:hs_strong_disagree``; ``downgrade:...`` kept whole). Numbers: the between-model
    disagreement (``hs_lr`` log ratio of Hs, ``dir_d`` deg, ``tp_d`` s, ``wind_d`` km/h),
    the ensemble's relative Hs spread (``spread``), the independent family count (``fam``),
    the run-to-run change (``run_change``) and the research assessment id (``assess``).
    ``stale``, ``missing`` and ``wind_dispute`` appear only when set. Absent values are left
    out, never stored as 0. None stays None.
    """
    if not inputs:
        return inputs
    disagreement = inputs.get("disagreement") or {}
    ensemble = inputs.get("ensemble") or {}
    local = inputs.get("local") or {}
    rules = [str(r) for r in inputs.get("rules") or []]
    digest: dict[str, Any] = {
        "digest": DIGEST_VERSION,
        "caps": [r.removeprefix("cap:") for r in rules if not r.startswith("note:")],
        "hs_lr": disagreement.get("hs_log_ratio"),
        "dir_d": disagreement.get("dir_diff_deg"),
        "tp_d": disagreement.get("tp_diff_s"),
        "wind_d": disagreement.get("wind_diff_kmh"),
        "spread": ensemble.get("rel_spread"),
        "fam": inputs.get("independent_families"),
        "run_change": inputs.get("run_change_ratio"),
        "assess": local.get("assessment_id"),
        "stale": inputs.get("stale") or None,
        "missing": inputs.get("missing") or None,
        "wind_dispute": True if inputs.get("wind_dispute") else None,
    }
    return {k: v for k, v in digest.items() if v is not None and v != []}


def _research_class(inputs: dict[str, Any] | None) -> str | None:
    """The overall research class local confidence used (None: no assessment existed)."""
    value = ((inputs or {}).get("local") or {}).get("research_class")
    return str(value) if value else None


def archive_authoritative(db: Session, now: datetime | None = None) -> dict[str, int]:
    use_digest = get_settings().archive_confidence_digest
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
                    "forecast_confidence_class": pred.forecast_confidence_class,
                    "local_confidence_class": pred.local_confidence_class,
                    "research_class": _research_class(pred.confidence_inputs),
                    "confidence_method": pred.confidence_method,
                    "confidence_inputs": (
                        confidence_digest(pred.confidence_inputs)
                        if use_digest
                        else pred.confidence_inputs
                    ),
                }
            )
        # RETURNING counts the rows actually inserted (conflicts return nothing).
        stmt = (
            insert(PredictionArchive)
            .on_conflict_do_nothing(
                index_elements=["source_code", "run_key", "spot_id", "valid_time"]
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
