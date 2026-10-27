"""Prediction archive: an immutable record of every forecast issued to users.

Forecast runs (and their hourly rows) are deleted after RETENTION_FORECAST_DAYS, and the
values a model forecast at a given lead time cannot be reconstructed later. So, when
PREDICTION_ARCHIVE_ENABLED is on (off by default, design D5; on in the full-retention
profile), after each prediction job the served run of every spot (the published forecast
version's, ``versions.serving_runs``) is copied into ``prediction_archive``
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
1,222 B of a 1,506 B row, the digest 278 B, so a row is 562 B (docs/operations.md).

Database egress (docs/map-layers.md, "Database egress, measured"): the archive reads only
what it stores. The 3-hourly valid times and the lead window are selected in SQL, and the
query returns plain rows of the archived columns (:data:`ARCHIVED_COLUMNS`), not ORM
entities: a column that is not selected raises AttributeError instead of loading lazily,
nothing is left half-loaded in the session (``load_only(..., raiseload=True)`` entities
would stay in the identity map and raise for a later ``db.get`` of the same row), and the
prediction's joined ``forecast`` relationship, which repeated every wave_forecasts column,
is not read. One demo pass: 88.1 MB before, 5.1 MB after. With ARCHIVE_CONFIDENCE_DIGEST the
confidence inputs are not read whole either, only the parts the digest keeps
(:data:`DIGEST_READS`, :func:`_digest_columns`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, case, cast, extract, func, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import ForecastRun, PredictionArchive, SurfQualityPrediction, WaveForecast
from app.services.forecasts.versions import serving_runs

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


# The columns archive_authoritative reads: the archived prediction values and the offshore
# inputs (no name is in both tables). The confidence inputs are read whole only without
# ARCHIVE_CONFIDENCE_DIGEST; with it, only the parts the digest keeps (DIGEST_READS).
ARCHIVED_VALUE_COLUMNS = (
    SurfQualityPrediction.spot_id,
    SurfQualityPrediction.valid_time,
    SurfQualityPrediction.breaking_height_mid_ft,
    SurfQualityPrediction.breaking_p10_ft,
    SurfQualityPrediction.breaking_p90_ft,
    SurfQualityPrediction.uncertainty_kind,
    SurfQualityPrediction.score,
    SurfQualityPrediction.confidence,
    SurfQualityPrediction.breaking_height_method,
    SurfQualityPrediction.algorithm_version,
    SurfQualityPrediction.ml_model_version,
    SurfQualityPrediction.forecast_confidence_class,
    SurfQualityPrediction.local_confidence_class,
    SurfQualityPrediction.confidence_method,
    *(getattr(WaveForecast, k) for k in OFFSHORE_FIELDS),
)
ARCHIVED_COLUMNS = (*ARCHIVED_VALUE_COLUMNS, SurfQualityPrediction.confidence_inputs)

DIGEST_VERSION = 1

# What confidence_digest and _research_class read from a prediction's confidence inputs:
# top-level keys whole (None) or the named keys of a nested object. Measured on the replayed
# 2026-10-16 blend (docs/map-layers.md, "Free database budget"): the archive's read of its
# 4,050 rows took 5.96 MB with the whole inputs (about 1.1 kB a prediction) and 2.45 MB with
# these parts.
DIGEST_READS: dict[str, tuple[str, ...] | None] = {
    "class": None,
    "rules": None,
    "independent_families": None,
    "run_change_ratio": None,
    "stale": None,
    "missing": None,
    "wind_dispute": None,
    "local": ("class", "assessment_id", "research_class"),
    "disagreement": ("hs_log_ratio", "dir_diff_deg", "tp_diff_s", "wind_diff_kmh"),
    "ensemble": ("rel_spread",),
}


def _digest_part_name(key: str, sub: str | None = None) -> str:
    return f"ci_{key}" if sub is None else f"ci_{key}__{sub}"


def _digest_columns() -> list[Any]:
    """The confidence inputs' parts of :data:`DIGEST_READS` as labelled columns, plus
    ``ci_object`` (true for a non-empty JSON object) and ``ci_other``: the whole value when
    it is anything else (SQL NULL, ``{}``, a JSON null), which confidence_digest returns as
    it is, so no row needs its whole inputs to be read."""
    inputs = SurfQualityPrediction.confidence_inputs
    is_object = (func.jsonb_typeof(inputs) == "object") & (inputs != cast({}, JSONB))
    columns: list[Any] = [
        func.coalesce(is_object, False).label("ci_object"),
        case((is_object, None), else_=inputs).label("ci_other"),
    ]
    for key, subs in DIGEST_READS.items():
        if subs is None:
            columns.append(inputs[key].label(_digest_part_name(key)))
        else:
            columns += [inputs[key][sub].label(_digest_part_name(key, sub)) for sub in subs]
    return columns


def _digest_inputs(row: Any) -> dict[str, Any] | None:
    """The confidence inputs of a :func:`_digest_columns` row, holding the parts
    confidence_digest and _research_class read and nothing else. A part the stored inputs
    lack (or hold as JSON null) is None here: both read every part with ``dict.get`` (a
    nested one after ``or {}``), which gives None for either."""
    values = row._mapping
    if not values["ci_object"]:
        other: dict[str, Any] | None = values["ci_other"]
        return other
    inputs: dict[str, Any] = {}
    for key, subs in DIGEST_READS.items():
        if subs is None:
            inputs[key] = values[_digest_part_name(key)]
        else:
            inputs[key] = {sub: values[_digest_part_name(key, sub)] for sub in subs}
    return inputs


def confidence_digest(inputs: dict[str, Any] | None) -> dict[str, Any] | None:
    """The archived digest of a prediction's ``confidence_inputs`` (see the module docstring).

    Kept under the full inputs' own names: ``class`` and ``local`` (its ``class`` and
    ``assessment_id``), so a reader of the full inputs reads these from a digest too.
    ``caps``: every rule except ``note:`` ones (``cap:`` dropped from the name, e.g.
    ``low:hs_strong_disagree``; ``downgrade:...`` kept whole). Numbers: the between-model
    disagreement (``hs_lr`` log ratio of Hs, ``dir_d`` deg, ``tp_d`` s, ``wind_d`` km/h),
    the ensemble's relative Hs spread (``spread``), the independent family count (``fam``)
    and the run-to-run change (``run_change``). ``stale``, ``missing`` and ``wind_dispute``
    appear only when set. Absent values are left out, never stored as 0. None stays None.
    """
    if not inputs:
        return inputs
    disagreement = inputs.get("disagreement") or {}
    ensemble = inputs.get("ensemble") or {}
    local = inputs.get("local") or {}
    rules = [str(r) for r in inputs.get("rules") or []]
    local_digest = {k: v for k in ("class", "assessment_id") if (v := local.get(k)) is not None}
    digest: dict[str, Any] = {
        "digest": DIGEST_VERSION,
        "class": inputs.get("class"),
        "local": local_digest or None,
        "caps": [r.removeprefix("cap:") for r in rules if not r.startswith("note:")],
        "hs_lr": disagreement.get("hs_log_ratio"),
        "dir_d": disagreement.get("dir_diff_deg"),
        "tp_d": disagreement.get("tp_diff_s"),
        "wind_d": disagreement.get("wind_diff_kmh"),
        "spread": ensemble.get("rel_spread"),
        "fam": inputs.get("independent_families"),
        "run_change": inputs.get("run_change_ratio"),
        "stale": inputs.get("stale") or None,
        "missing": inputs.get("missing") or None,
        "wind_dispute": True if inputs.get("wind_dispute") else None,
    }
    return {k: v for k, v in digest.items() if v is not None and v != []}


def _research_class(inputs: dict[str, Any] | None) -> str | None:
    """The overall research class local confidence used (None: no assessment existed)."""
    value = ((inputs or {}).get("local") or {}).get("research_class")
    return str(value) if value else None


def _archived_rows(
    run_id: int, spot_ids: list[int], issued_at: datetime, *, digest: bool = False
) -> Select[Any]:
    """The run's archived rows (:data:`ARCHIVED_COLUMNS`; with ``digest`` the confidence
    inputs as :func:`_digest_columns` instead): valid times on the 3-hourly UTC grid
    (ARCHIVE_STEP_HOURS) from 6 h before ``issued_at`` to ARCHIVE_MAX_LEAD_HOURS after it,
    inclusive, for ``spot_ids``."""
    hour = extract("hour", func.timezone("UTC", SurfQualityPrediction.valid_time))
    columns = (*ARCHIVED_VALUE_COLUMNS, *_digest_columns()) if digest else ARCHIVED_COLUMNS
    return (
        select(*columns)
        .join(WaveForecast, WaveForecast.id == SurfQualityPrediction.forecast_id)
        .where(
            SurfQualityPrediction.run_id == run_id,
            SurfQualityPrediction.spot_id.in_(spot_ids),
            hour % ARCHIVE_STEP_HOURS == 0,
            SurfQualityPrediction.valid_time >= issued_at - timedelta(hours=6),
            SurfQualityPrediction.valid_time <= issued_at + timedelta(hours=ARCHIVE_MAX_LEAD_HOURS),
        )
    )


def served_runs(db: Session, now: datetime | None = None) -> dict[int, ForecastRun]:
    """{spot_id: run} of the published forecast version (``versions.serving_runs``; before
    any version, the legacy selection ``latest_runs_by_spot``, whose runs are then already
    in the session)."""
    by_spot = serving_runs(db, now=now)
    runs = {run_id: db.get(ForecastRun, run_id) for run_id in set(by_spot.values())}
    return {spot: run for spot, run_id in by_spot.items() if (run := runs[run_id]) is not None}


def archive_authoritative(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Archive the served run of every spot (the caller decides whether the archive is on:
    PREDICTION_ARCHIVE_ENABLED, ``surf_quality.predict.compute_pending_predictions``)."""
    use_digest = get_settings().archive_confidence_digest
    chosen = served_runs(db, now)
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
        for pred in db.execute(_archived_rows(run_id, spot_ids, run.issued_at, digest=use_digest)):
            inputs = _digest_inputs(pred) if use_digest else pred.confidence_inputs
            lead = (pred.valid_time - run.issued_at).total_seconds() / 3600.0
            rows.append(
                {
                    "spot_id": pred.spot_id,
                    "source_code": run.source.code,
                    "run_key": run.run_key,
                    "issued_at": run.issued_at,
                    "valid_time": pred.valid_time,
                    "lead_hours": round(lead, 2),
                    **{k: getattr(pred, k) for k in OFFSHORE_FIELDS},
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
                    "research_class": _research_class(inputs),
                    "confidence_method": pred.confidence_method,
                    "confidence_inputs": confidence_digest(inputs) if use_digest else inputs,
                }
            )
        # RETURNING counts the rows actually inserted (conflicts return nothing).
        stmt = (
            insert(PredictionArchive)
            .on_conflict_do_nothing(
                index_elements=["source_code", "run_key", "spot_id", "valid_time"]
            )
            .returning(PredictionArchive.id)
            .execution_options(render_nulls=True)  # one batched statement per chunk
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
