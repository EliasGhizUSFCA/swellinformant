"""Job C — detect and maintain swell events from the latest predictions.

Events keep a stable identity across model runs: a new cluster that overlaps an existing
ACTIVE event (±12 h) updates that event in place (``version`` increments and a snapshot is
appended to ``history``). Clusters overlapping several events merge them (the extras are
CANCELLED with ``merged_into_id``). Active events that the latest run no longer supports
are DOWNGRADED; events whose end time has passed become PASSED. A per-spot transaction
plus the deferred exclusion constraint guarantee no duplicate overlapping active events.

The peak hour's forecast and local confidence classes, reasons and lead time
(forecast-v1) are stored in each history snapshot; the newest snapshot is the event's
current assessment (``peak_confidence``), so no extra column is needed. Snapshots also keep
the forecast-only reasons, the disputed-wind note and the peak hour's wind strength,
favourability and gust definition (``peak_wind``); older snapshots without them get the
reasons split from the mixed list and no wind regime.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import ForecastRun, SurfQualityPrediction, SurfSpot, SwellEvent, WaveForecast
from app.models.enums import QualityLabel, SwellEventStatus
from app.services.forecasts.queries import latest_runs_by_spot
from app.services.surf_quality.breaking import dominant_component
from app.services.surf_quality.confidence import confidence_label
from app.services.surf_quality.forecast_confidence import (
    WIND_DISPUTE_REASON,
    api_fields,
    cap_for_staleness,
    forecast_only,
)
from app.services.surf_quality.scoring import label_for_score
from app.services.swell_detection.clustering import Cluster, Step, cluster_steps, overlaps

logger = logging.getLogger(__name__)

MATCH_TOLERANCE = timedelta(hours=12)


@dataclass
class DetectionStats:
    spots: int = 0
    created: int = 0
    updated: int = 0
    merged: int = 0
    downgraded: int = 0
    passed: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return self.__dict__.copy()


def load_steps(db: Session, spot_id: int, run_id: int, since: datetime) -> list[Step]:
    rows = db.execute(
        select(SurfQualityPrediction, WaveForecast)
        .join(WaveForecast, WaveForecast.id == SurfQualityPrediction.forecast_id)
        .where(
            SurfQualityPrediction.run_id == run_id,
            SurfQualityPrediction.spot_id == spot_id,
            SurfQualityPrediction.valid_time >= since,
        )
        .order_by(SurfQualityPrediction.valid_time)
    ).all()
    return [
        Step(time=p.valid_time, is_daylight=p.is_daylight, score=p.score, payload=(p, f))
        for p, f in rows
    ]


# Peak-hour confidence fields of EventOut, and what each history snapshot stores.
EVENT_CONFIDENCE_KEYS = (
    "forecast_confidence_class",
    "local_confidence_class",
    "confidence_reasons",
    "local_confidence_reason",
    "forecast_confidence_reasons",
    "wind_dispute_note",
)
SNAPSHOT_CONFIDENCE_KEYS = (*EVENT_CONFIDENCE_KEYS, "lead_hours")
# Peak-hour wind regime of EventOut (from the prediction's stored wind_detail).
EVENT_WIND_KEYS = ("wind_strength", "wind_favourability", "gust_kind")


def _confidence_snapshot(peak: SurfQualityPrediction) -> dict[str, Any]:
    """Forecast-v1 confidence of the event's peak hour (empty for older predictions)."""
    if peak.forecast_confidence_class is None:
        return {}
    fields = api_fields(peak)
    return {
        "confidence_method": peak.confidence_method,
        **{k: fields[k] for k in SNAPSHOT_CONFIDENCE_KEYS},
    }


def _wind_snapshot(peak: SurfQualityPrediction, fc: WaveForecast) -> dict[str, Any]:
    """Wind strength, favourability and gust definition of the peak hour (None values are
    left out, so older predictions add nothing)."""
    from app.services.spot_views import wind_fields  # spot_views imports this module

    return {k: v for k, v in wind_fields(peak.components, fc.extra).items() if v is not None}


def peak_confidence(event: SwellEvent, *, stale: bool = False) -> dict[str, Any]:
    """EventOut's peak-hour confidence fields from the newest history snapshot (may be empty
    for events last updated before forecast-v1). ``stale``: the event's run has gone stale
    since detection, so the read-time staleness rule applies, exactly as for the same hour
    in the forecast (``forecast_confidence.cap_for_staleness``). Snapshots taken before the
    reason split get ``forecast_confidence_reasons`` and ``wind_dispute_note`` from their
    mixed ``confidence_reasons``."""
    latest = (event.history or [])[-1:] or [{}]
    fields = {k: latest[0][k] for k in EVENT_CONFIDENCE_KEYS if k in latest[0]}
    mixed = [str(r) for r in fields.get("confidence_reasons") or []]
    if "confidence_reasons" in fields and "forecast_confidence_reasons" not in fields:
        fields["forecast_confidence_reasons"] = forecast_only(mixed)
    if "confidence_reasons" in fields and "wind_dispute_note" not in fields:
        fields["wind_dispute_note"] = WIND_DISPUTE_REASON if WIND_DISPUTE_REASON in mixed else None
    return cap_for_staleness(fields) if stale else fields


def peak_wind(event: SwellEvent) -> dict[str, Any]:
    """EventOut's peak-hour wind strength, favourability and gust definition from the
    newest history snapshot (absent keys stay None)."""
    latest = (event.history or [])[-1:] or [{}]
    return {k: latest[0][k] for k in EVENT_WIND_KEYS if k in latest[0]}


def _apply_cluster(event: SwellEvent, cluster: Cluster, run: ForecastRun, keep_start: bool) -> None:
    peak_pred, peak_fc = cluster.peak.payload
    preds = [s.payload[0] for s in cluster.steps]
    if not keep_start or cluster.start < event.start_time:
        event.start_time = cluster.start
    event.end_time = cluster.end
    event.peak_time = peak_pred.valid_time
    event.peak_score = peak_pred.score
    event.avg_score = round(cluster.avg_score, 1)
    event.peak_label = QualityLabel(label_for_score(peak_pred.score))
    event.peak_breaking_height_min_ft = peak_pred.breaking_height_min_ft
    event.peak_breaking_height_max_ft = peak_pred.breaking_height_max_ft
    (
        event.peak_swell_height_m,
        event.peak_swell_period_s,
        event.peak_swell_direction_deg,
    ) = dominant_component(peak_pred, peak_fc)
    event.peak_wind_speed_kmh = peak_fc.wind_speed_kmh
    event.peak_wind_direction_deg = peak_fc.wind_direction_deg
    event.peak_wind_relation = peak_pred.wind_relation
    event.qualifying_hours = int(round(cluster.qualifying_hours))
    event.confidence = int(round(mean(p.confidence for p in preds)))
    event.confidence_label = confidence_label(event.confidence)
    event.latest_run_id = run.id
    event.source_code = run.source.code
    event.is_demo = run.source.is_demo
    event.last_updated_at = utcnow()
    snapshot = {
        "run_key": run.run_key,
        "at": event.last_updated_at.isoformat(),
        "start": event.start_time.isoformat(),
        "end": event.end_time.isoformat(),
        "peak_score": event.peak_score,
        "peak_height_ft": [event.peak_breaking_height_min_ft, event.peak_breaking_height_max_ft],
        "confidence": event.confidence,
        **_confidence_snapshot(peak_pred),
        **_wind_snapshot(peak_pred, peak_fc),
    }
    event.history = [*(event.history or [])[-19:], snapshot]


def detect_for_spot(
    db: Session, spot: SurfSpot, run: ForecastRun, now: datetime, stats: DetectionStats
) -> list[SwellEvent]:
    settings = get_settings()
    steps = load_steps(db, spot.id, run.id, since=now - timedelta(hours=6))
    threshold = settings.swell_event_min_score
    clusters = cluster_steps(
        steps,
        qualifies=lambda s: s.score >= threshold,
        min_hours=settings.swell_event_min_hours,
        break_daylight_hours=settings.swell_event_break_daylight_hours,
    )
    coverage_start = steps[0].time if steps else now

    existing = list(
        db.scalars(
            select(SwellEvent)
            .where(SwellEvent.spot_id == spot.id, SwellEvent.status == SwellEventStatus.ACTIVE)
            .order_by(SwellEvent.start_time)
            .with_for_update(of=SwellEvent)
        ).all()
    )
    touched: set[object] = set()
    results: list[SwellEvent] = []
    for cluster in clusters:
        hits = [
            e
            for e in existing
            if e.id not in touched
            and overlaps(e.start_time, e.end_time, cluster.start, cluster.end, MATCH_TOLERANCE)
        ]
        if hits:
            primary = hits[0]
            for extra in hits[1:]:
                extra.status = SwellEventStatus.CANCELLED
                extra.merged_into_id = primary.id
                extra.last_updated_at = utcnow()
                touched.add(extra.id)
                stats.merged += 1
            keep_start = primary.start_time < coverage_start
            _apply_cluster(primary, cluster, run, keep_start)
            primary.version += 1
            touched.add(primary.id)
            stats.updated += 1
            results.append(primary)
        else:
            peak_pred = cluster.peak.payload[0]
            event = SwellEvent(
                spot_id=spot.id,
                status=SwellEventStatus.ACTIVE,
                start_time=cluster.start,
                end_time=cluster.end,
                peak_time=peak_pred.valid_time,
                peak_score=peak_pred.score,
                avg_score=0.0,
                peak_label=QualityLabel(label_for_score(peak_pred.score)),
                peak_breaking_height_min_ft=0,
                peak_breaking_height_max_ft=0,
                qualifying_hours=0,
                confidence=0,
                confidence_label=confidence_label(0),
                source_code=run.source.code,
                first_detected_at=utcnow(),
                version=1,
                history=[],
            )
            _apply_cluster(event, cluster, run, keep_start=False)
            db.add(event)
            stats.created += 1
            results.append(event)
    for e in existing:
        if e.id in touched:
            continue
        e.last_updated_at = utcnow()
        if e.end_time < now:
            e.status = SwellEventStatus.PASSED
            stats.passed += 1
        else:
            e.status = SwellEventStatus.DOWNGRADED
            e.latest_run_id = run.id
            stats.downgraded += 1
    return results


def expire_passed_events(db: Session, now: datetime) -> int:
    result = db.execute(
        update(SwellEvent)
        .where(SwellEvent.status == SwellEventStatus.ACTIVE, SwellEvent.end_time < now)
        .values(status=SwellEventStatus.PASSED, last_updated_at=now)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def detect_events(db: Session, now: datetime | None = None) -> DetectionStats:
    now = now or utcnow()
    stats = DetectionStats()
    runs = latest_runs_by_spot(db, now)
    spots = db.scalars(select(SurfSpot).where(SurfSpot.is_active)).all()
    for spot in spots:
        run = runs.get(spot.id)
        if run is None:
            continue
        stats.spots += 1
        try:
            detect_for_spot(db, spot, run, now, stats)
            db.commit()  # one transaction per spot: the deferred constraint is checked here
        except Exception:
            db.rollback()
            stats.errors += 1
            logger.exception("Swell detection failed for %s; other spots continue", spot.slug)
    stats.passed += expire_passed_events(db, now)
    db.commit()
    logger.info("Swell detection: %s", stats.as_dict())
    return stats
