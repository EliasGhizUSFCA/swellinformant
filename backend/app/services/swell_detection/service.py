"""Job C — detect and maintain swell events from the latest predictions.

Events keep a stable identity across model runs: a new cluster that overlaps an existing
ACTIVE event (±12 h) updates that event in place (``version`` increments and a snapshot is
appended to ``history``); clusters and events are paired one to one, best overlap first
(:func:`match_clusters`). A cluster that also overlaps events no other cluster continues
merges them (the extras are CANCELLED with ``merged_into_id``). Active events that the
latest run no longer supports are DOWNGRADED; events whose end time has passed become
PASSED. A per-spot transaction plus the deferred exclusion constraint guarantee no duplicate
overlapping active events.

The peak hour's forecast and local confidence classes, reasons and lead time
(forecast-v1) are stored in each history snapshot; the newest snapshot is the event's
current assessment (``peak_confidence``), so no extra column is needed. Snapshots also keep
the forecast-only reasons, the disputed-wind note and the peak hour's wind strength,
favourability and gust definition (``peak_wind``); older snapshots without them get the
reasons split from the mixed list and no wind regime.

Database egress (docs/map-layers.md, "Database egress, measured"): :func:`load_steps`, which
detection and matching call for every spot and event, reads only the columns the steps are
judged on (:class:`StepPrediction`, :class:`StepForecast`): about 140 B a step on the wire
instead of 4.6 kB for the whole prediction, its forecast and that forecast again through
the prediction's joined relationship (one demo pass: 88.2 MB before, 2.7 MB after). An
event's peak is then read in full, one query per spot (:func:`_peak_predictions`), for the
snapshot's confidence, wind and dominant-component fields.
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
from app.services import timesteps
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
from app.services.surf_quality.sun import daylight_windows
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


@dataclass(frozen=True, slots=True)
class StepPrediction:
    """The prediction columns a step is judged on (detection, matching.evaluate).

    ``components`` holds only the ``consistency`` share (matching's min_consistency rule),
    never the full breakdown: read the stored prediction by ``id`` for anything else (as
    detection does for an event's peak). Being slotted, an attribute that was not selected
    raises AttributeError instead of loading anything.
    """

    id: int
    forecast_id: int
    valid_time: datetime
    is_daylight: bool
    score: int
    confidence: int
    breaking_height_min_ft: float
    breaking_height_max_ft: float
    breaking_height_mid_ft: float | None
    wind_relation: str
    components: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StepForecast:
    """The offshore forecast columns a step is judged on (matching.evaluate)."""

    wave_period_s: float | None
    primary_swell_period_s: float | None
    primary_swell_direction_deg: float | None
    wind_speed_kmh: float | None


_STEP_PREDICTION_COLUMNS = (
    SurfQualityPrediction.id,
    SurfQualityPrediction.forecast_id,
    SurfQualityPrediction.valid_time,
    SurfQualityPrediction.is_daylight,
    SurfQualityPrediction.score,
    SurfQualityPrediction.confidence,
    SurfQualityPrediction.breaking_height_min_ft,
    SurfQualityPrediction.breaking_height_max_ft,
    SurfQualityPrediction.breaking_height_mid_ft,
    SurfQualityPrediction.wind_relation,
)
_STEP_FORECAST_COLUMNS = (
    WaveForecast.wave_period_s,
    WaveForecast.primary_swell_period_s,
    WaveForecast.primary_swell_direction_deg,
    WaveForecast.wind_speed_kmh,
)


_STEP_PREDICTION_NAMES = tuple(c.key for c in _STEP_PREDICTION_COLUMNS)
_STEP_FORECAST_NAMES = tuple(c.key for c in _STEP_FORECAST_COLUMNS)


def _step_payload(row: Any) -> tuple[StepPrediction, StepForecast]:
    """(prediction columns, components['consistency'], forecast columns) -> payload."""
    n = len(_STEP_PREDICTION_COLUMNS)
    consistency = row[n]
    pred = StepPrediction(
        **dict(zip(_STEP_PREDICTION_NAMES, row[:n], strict=True)),
        components={} if consistency is None else {"consistency": consistency},
    )
    fc = StepForecast(**dict(zip(_STEP_FORECAST_NAMES, row[n + 1 :], strict=True)))
    return pred, fc


def load_steps(db: Session, spot_id: int, run_id: int, since: datetime) -> list[Step]:
    """The run's steps for the spot from ``since``, each with its real length: ``duration``
    is the gap to the next stored time (the last keeps the spacing before it) and
    ``daylight`` the part of its nearest-valid-step interval between first and last light
    at the spot (``sun.daylight_windows``), as the spot timeline counts good hours.

    ``payload`` is ``(StepPrediction, StepForecast)``: only the columns steps are judged on
    are read (see the module docstring)."""
    rows = [
        _step_payload(row)
        for row in db.execute(
            select(
                *_STEP_PREDICTION_COLUMNS,
                SurfQualityPrediction.components["consistency"],
                *_STEP_FORECAST_COLUMNS,
            )
            .join(WaveForecast, WaveForecast.id == SurfQualityPrediction.forecast_id)
            .where(
                SurfQualityPrediction.run_id == run_id,
                SurfQualityPrediction.spot_id == spot_id,
                SurfQualityPrediction.valid_time >= since,
            )
            .order_by(SurfQualityPrediction.valid_time)
        )
    ]
    if not rows:
        return []
    times = [p.valid_time for p, _ in rows]
    after = db.scalar(
        select(SurfQualityPrediction.valid_time)
        .where(
            SurfQualityPrediction.run_id == run_id,
            SurfQualityPrediction.spot_id == spot_id,
            SurfQualityPrediction.valid_time > times[-1],
        )
        .order_by(SurfQualityPrediction.valid_time)
        .limit(1)
    )
    durations = timesteps.forward_hours(times, after)
    extents = timesteps.centred_extents(times, durations)
    spot = db.get(SurfSpot, spot_id)
    windows = (
        daylight_windows(
            spot.latitude, spot.longitude, extents[0][0] - timedelta(days=1), extents[-1][1]
        )
        if spot is not None
        else None
    )
    return [
        Step(
            time=p.valid_time,
            is_daylight=p.is_daylight,
            score=p.score,
            duration=timedelta(hours=hours),
            payload=(p, f),
            daylight=(
                timedelta(hours=timesteps.overlap_hours(extent, windows))
                if windows is not None
                else None
            ),
        )
        for (p, f), hours, extent in zip(rows, durations, extents, strict=True)
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


def _peak_predictions(db: Session, clusters: list[Cluster]) -> dict[int, SurfQualityPrediction]:
    """The stored predictions (with their forecasts) at the clusters' peaks, by id: one
    query for all of a spot's clusters."""
    ids = {c.peak.payload[0].id for c in clusters}
    if not ids:
        return {}
    return {
        p.id: p
        for p in db.scalars(
            select(SurfQualityPrediction).where(SurfQualityPrediction.id.in_(ids))
        ).all()
    }


def _apply_cluster(
    event: SwellEvent,
    cluster: Cluster,
    run: ForecastRun,
    keep_start: bool,
    peak_pred: SurfQualityPrediction,
) -> None:
    """Update ``event`` from ``cluster``; ``peak_pred`` is the stored prediction at the
    cluster's peak (read in full: the snapshot needs its components and confidence)."""
    peak_fc = peak_pred.forecast
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


def _overlap_seconds(event: SwellEvent, cluster: Cluster) -> float:
    """How far an event and a cluster overlap (negative: the gap between them)."""
    return (min(event.end_time, cluster.end) - max(event.start_time, cluster.start)).total_seconds()


def match_clusters(
    clusters: list[Cluster], existing: list[SwellEvent]
) -> tuple[list[int | None], dict[int, int]]:
    """Which active event each cluster continues, and which events merge into which cluster.

    ``existing`` is ordered by (start_time, id). First one to one: the (cluster, event)
    pairs that overlap within MATCH_TOLERANCE are taken best overlap first, each cluster and
    each event at most once. Then an event that no cluster took merges into the cluster it
    overlaps most, if any (that cluster always has an event: a pair whose cluster and event
    were both free would have been taken). Of the events one cluster absorbs, the earliest
    continues and the others are cancelled into it, as before Upgrade 4's integration: the
    swell first seen keeps its identity and its start from before the run's coverage.

    Returns the event index per cluster (None: a new event) and {event index: cluster
    index} for the merges. A cluster that reaches past its own event into a neighbour's
    (the ±12 h tolerance) therefore never takes the neighbour's event from the cluster that
    continues it, so detection over unchanged data keeps every event (it used to cancel the
    second of two close events into the first and create it again on every pass).
    """
    pairs = sorted(
        (-_overlap_seconds(e, c), ei, ci)
        for ci, c in enumerate(clusters)
        for ei, e in enumerate(existing)
        if overlaps(e.start_time, e.end_time, c.start, c.end, MATCH_TOLERANCE)
    )
    primary: list[int | None] = [None] * len(clusters)
    taken: set[int] = set()
    for _, ei, ci in pairs:
        if primary[ci] is None and ei not in taken:
            primary[ci] = ei
            taken.add(ei)
    merges: dict[int, int] = {}
    for _, ei, ci in pairs:
        if ei not in taken and ei not in merges:
            merges[ei] = ci
    for ei, ci in list(merges.items()):
        first = primary[ci]
        if first is not None and ei < first:  # an earlier event continues instead
            del merges[ei]
            merges[first] = ci
            primary[ci] = ei
    return primary, merges


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
    peaks = _peak_predictions(db, clusters)

    existing = list(
        db.scalars(
            select(SwellEvent)
            .where(SwellEvent.spot_id == spot.id, SwellEvent.status == SwellEventStatus.ACTIVE)
            .order_by(SwellEvent.start_time, SwellEvent.id)
            .with_for_update(of=SwellEvent)
        ).all()
    )
    matched, merges = match_clusters(clusters, existing)
    touched: set[object] = set()
    results: list[SwellEvent] = []
    for ci, cluster in enumerate(clusters):
        if (index := matched[ci]) is not None:
            primary = existing[index]
            for ei in sorted(ei for ei, target in merges.items() if target == ci):
                extra = existing[ei]
                extra.status = SwellEventStatus.CANCELLED
                extra.merged_into_id = primary.id
                extra.last_updated_at = utcnow()
                touched.add(extra.id)
                stats.merged += 1
            # An event that began before the run's coverage keeps its start only when it
            # continues the first cluster: kept by a later one it would span the clusters
            # before it (and overlap their events).
            keep_start = ci == 0 and primary.start_time < coverage_start
            _apply_cluster(primary, cluster, run, keep_start, peaks[cluster.peak.payload[0].id])
            primary.version += 1
            touched.add(primary.id)
            stats.updated += 1
            results.append(primary)
        else:
            peak_pred = peaks[cluster.peak.payload[0].id]
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
            _apply_cluster(event, cluster, run, False, peak_pred)
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
