"""The admin freshness dashboard (spec section 16, design D12, contract C5).

:func:`freshness_report` builds the body of GET /api/admin/freshness from what the pipeline
already records; it never calls a provider:

* the forecast versions (``forecast_versions``: the newest
  app.services.health.VERSION_HISTORY by id, without their spot maps, and the published
  one): component model runs, detection, acquisition, scoring and publication times, spot
  counts, the freshness timeline and the upstream-availability-to-publication latency;
* the syncs (``sync_runs``: the newest REFRESH_HISTORY by start): the refresh jobs with
  their trigger, stages, durations and redacted error, and the processing time per stage;
* the forecast runs (the newest app.services.usage.RECENT_RUNS by id): upstream requests,
  HTTP statuses and weighted calls of the last 24 h, the last acquisition and scoring when
  no version exists yet;
* Redis: the API counters (app.services.usage), the version marker's first-served times and
  the browsers' seen latencies (app.services.forecasts.versions), the refresh checks and
  the provider health;
* the data-health report (app.services.health.data_health) for the public problems, and
  the admin-only problems (app.services.health.evaluate_admin).

Every query is bounded (newest N rows through an index); the page is read by admins only
and nothing here runs for a public request.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.core.logging import redact
from app.models import ForecastRun, ForecastSource
from app.models.sync import SyncRun
from app.models.version import ForecastVersion
from app.schemas.admin import (
    AdminActiveProviderOut,
    AdminApiUsageOut,
    AdminComponentFreshnessOut,
    AdminDomainOut,
    AdminDurationStatsOut,
    AdminEmailUsageOut,
    AdminEventOut,
    AdminFailedRequestOut,
    AdminFlightUsageOut,
    AdminFreshnessOut,
    AdminLatencyOut,
    AdminOpenMeteoUsageOut,
    AdminProblemOut,
    AdminProcessingTimeOut,
    AdminProviderHealthOut,
    AdminProviderRequestsOut,
    AdminPublishedVersionOut,
    AdminRefreshJobOut,
    AdminSchedulerCheckOut,
    AdminSchedulerOut,
    AdminStageOut,
    AdminStageTimeOut,
    AdminStaleSpotOut,
    AdminUsageWindowOut,
    AdminVersionCountsOut,
    AdminVersionRowOut,
    AdminWindyUsageOut,
)
from app.services import health
from app.services.forecasts import versions
from app.services.forecasts.base import ATMOSPHERE, WAVE
from app.services.forecasts.queries import USABLE
from app.services.forecasts.registry import configured_source_codes
from app.services.sync import state
from app.services.usage import RECENT_RUNS

FIRST_SERVED_VERSIONS = 5  # newest published versions whose first-served time is read
ERROR_MAX_CHARS = 500
PUBLISHED_STATUSES = (versions.PUBLISHED, versions.SUPERSEDED)
NOTES = [
    "Open-Meteo counts weighted calls (more than 10 variables or 14 days per location "
    "count extra); its metadata calls are free and not counted.",
    "Counters live in Redis and restart from zero when Redis loses its data.",
    "Latencies are measured: availability is the provider's own last_run_availability_time "
    "(Windy: the first sighting of a new data fingerprint, an estimate).",
]


# --------------------------------------------------------------------------- statistics
def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (``q`` in 0..1) of ``values``; None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def duration_stats(values: Sequence[float], *, last: float | None = None) -> AdminDurationStatsOut:
    """``values`` newest first (``last`` defaults to the first)."""
    if not values:
        return AdminDurationStatsOut()
    return AdminDurationStatsOut(
        samples=len(values),
        last=round(values[0] if last is None else last, 3),
        avg=round(sum(values) / len(values), 3),
        p50=_round(percentile(values, 0.5)),
        p95=_round(percentile(values, 0.95)),
        max=round(max(values), 3),
    )


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _gap(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None or end < start:
        return None
    return (end - start).total_seconds()


# --------------------------------------------------------------------------- blocks
def _active_provider(selection: Mapping[str, Any]) -> AdminActiveProviderOut:
    providers = [str(p) for p in selection.get("providers") or []]
    health_rows = []
    for name, h in (selection.get("health") or {}).items():
        if not isinstance(h, Mapping):
            continue
        health_rows.append(
            AdminProviderHealthOut(
                provider=str(h.get("provider") or name),
                last_success_at=health.as_datetime(h.get("last_success_at")),
                last_failure_at=health.as_datetime(h.get("last_failure_at")),
                last_failure_kind=h.get("last_failure_kind"),
                last_failure_detail=(
                    redact(str(h["last_failure_detail"]))[:ERROR_MAX_CHARS]
                    if h.get("last_failure_detail")
                    else None
                ),
                consecutive_unavailable=int(h.get("consecutive_unavailable") or 0),
                blocked_until=health.as_datetime(h.get("blocked_until")),
                blocking=bool(h.get("blocking")),
            )
        )
    return AdminActiveProviderOut(
        primary=str(selection.get("primary") or (providers[0] if providers else "")),
        providers=providers,
        configured=[str(p) for p in selection.get("configured") or providers],
        source=str(selection.get("source") or ""),
        weather_provider=str(selection.get("weather_provider") or ""),
        windy_eligible=bool(selection.get("windy_eligible")),
        fallback=selection.get("fallback"),
        fallback_active=bool(selection.get("fallback_from")),
        fallback_from=selection.get("fallback_from"),
        fallback_until=health.as_datetime(selection.get("fallback_until")),
        reasons=[str(r) for r in selection.get("reasons") or []],
        health=health_rows,
    )


def _component(
    t: health.ComponentTiming, current: ForecastVersion | None
) -> AdminComponentFreshnessOut:
    served = versions.component_runs_of(current).get(t.component) if current else None
    return AdminComponentFreshnessOut(
        component=t.component,  # type: ignore[arg-type]  # ATMOSPHERE or WAVE
        model=t.model,
        run_key=t.run_key,
        run=t.run,
        estimated=t.estimated,
        available_at=t.available_at,
        last_detected_at=t.last_detected_at,
        last_acquired_at=t.retrieved_at,
        changed=t.changed,
        carried_from_run_id=t.carried_from_run_id,
        newest_run=t.newest_run,
        update_interval_minutes=round(t.interval_s / 60, 1),
        update_interval_recorded=t.interval_recorded,
        typical_lag_minutes=None if t.typical_lag_s is None else round(t.typical_lag_s / 60, 1),
        lag_samples=t.lag_samples,
        expected_next_at=t.expected_next_at,
        late_after=t.late_after,
        late=t.late,
        domains=[
            AdminDomainOut(model=d.model, init_time=d.init_time, available_at=d.available_at)
            for d in (served.domains if served else ())
        ],
    )


def _changed(version: ForecastVersion) -> list[str]:
    return [
        c
        for c, run in sorted(versions.component_runs_of(version).items())
        if run.changed and c in (ATMOSPHERE, WAVE)
    ]


def _is_refresh_version(version: ForecastVersion) -> bool:
    """A version that published newly fetched model data (not a retry of failed spots,
    which keeps the published version's availability time)."""
    kind = (version.timings or {}).get("kind")
    return kind != "retry" and bool(_changed(version))


def _version_rows(history: Sequence[ForecastVersion]) -> list[AdminVersionRowOut]:
    return [
        AdminVersionRowOut(
            id=int(v.id),
            status=v.status,
            kind=(v.timings or {}).get("kind"),
            provider=v.provider,
            fallback_from=v.fallback_from,
            detected_at=v.detected_at,
            upstream_available_at=v.upstream_available_at,
            staged_at=v.staged_at,
            published_at=v.published_at,
            wind_model_run=v.wind_model_run,
            wave_model_run=v.wave_model_run,
            changed=_changed(v),  # type: ignore[arg-type]  # ATMOSPHERE or WAVE
            spots_updated=v.spots_updated,
            spots_stale=v.spots_stale,
            spots_failed=v.spots_failed,
            upstream_to_publication_s=(
                _gap(v.upstream_available_at, v.published_at) if _is_refresh_version(v) else None
            ),
        )
        for v in history
    ]


def _published(version: ForecastVersion | None, now: datetime) -> AdminPublishedVersionOut | None:
    if version is None:
        return None
    age = _gap(version.published_at, now)
    return AdminPublishedVersionOut(
        id=int(version.id),
        published_at=version.published_at,
        age_minutes=None if age is None else round(age / 60, 1),
        provider=version.provider,
        source_code=version.source_code,
        fallback_from=version.fallback_from,
        wind_model=version.wind_model,
        wind_model_run=version.wind_model_run,
        wind_run_estimated=version.wind_run_estimated,
        wave_model=version.wave_model,
        wave_model_run=version.wave_model_run,
        wave_run_estimated=version.wave_run_estimated,
        counts=AdminVersionCountsOut(
            total=version.spots_total,
            updated=version.spots_updated,
            stale=version.spots_stale,
            failed=version.spots_failed,
        ),
    )


def _recent_runs(db: Session, codes: Iterable[str]) -> list[tuple[str, Any, Any, Any]]:
    """(source code, status, completed_at, predictions_computed_at) of the newest
    RECENT_RUNS forecast runs of ``codes``."""
    wanted = sorted(set(codes))
    if not wanted:
        return []
    rows = db.execute(
        select(
            ForecastSource.code,
            ForecastRun.status,
            ForecastRun.completed_at,
            ForecastRun.predictions_computed_at,
        )
        .join(ForecastSource, ForecastSource.id == ForecastRun.source_id)
        .where(ForecastSource.code.in_(wanted))
        .order_by(ForecastRun.id.desc())
        .limit(RECENT_RUNS)
    ).all()
    return [(str(c), s, done, scored) for c, s, done, scored in rows]


def _events(
    history: Sequence[ForecastVersion], runs: Sequence[tuple[str, Any, Any, Any]]
) -> tuple[AdminEventOut, AdminEventOut, AdminEventOut]:
    """(last detected model update, last successful acquisition, last successful scoring):
    from the versions, else (no version yet) from the forecast runs."""
    detected = max(
        (v for v in history if v.detected_at is not None),
        key=lambda v: v.detected_at,  # type: ignore[arg-type, return-value]
        default=None,
    )
    published = [v for v in history if v.status in PUBLISHED_STATUSES]
    acquired = max(
        (v for v in published if v.acquired_at is not None),
        key=lambda v: v.acquired_at,  # type: ignore[arg-type, return-value]
        default=None,
    )
    scored = max(
        (v for v in published if v.scored_at is not None),
        key=lambda v: v.scored_at,  # type: ignore[arg-type, return-value]
        default=None,
    )

    def describe(v: ForecastVersion) -> str:
        changed = ", ".join(_changed(v)) or "no component"
        return f"{v.provider or v.source_code}: {changed} (version {v.id})"

    detected_out = (
        AdminEventOut(
            at=detected.detected_at, detail=describe(detected), version_id=int(detected.id)
        )
        if detected is not None
        else AdminEventOut()
    )
    run_done = max(
        ((code, done) for code, status, done, _ in runs if status in USABLE and done is not None),
        key=lambda r: r[1],
        default=None,
    )
    run_scored = max(
        ((code, at) for code, _, _, at in runs if at is not None),
        key=lambda r: r[1],
        default=None,
    )
    if acquired is not None:
        acquired_out = AdminEventOut(
            at=acquired.acquired_at, detail=describe(acquired), version_id=int(acquired.id)
        )
    elif run_done is not None:
        acquired_out = AdminEventOut(at=run_done[1], detail=f"{run_done[0]} (forecast run)")
    else:
        acquired_out = AdminEventOut()
    if scored is not None:
        scored_out = AdminEventOut(
            at=scored.scored_at, detail=describe(scored), version_id=int(scored.id)
        )
    elif run_scored is not None:
        scored_out = AdminEventOut(at=run_scored[1], detail=f"{run_scored[0]} (forecast run)")
    else:
        scored_out = AdminEventOut()
    return detected_out, acquired_out, scored_out


def _window(
    used: Any, limit: Any, resets_at: Any, fraction_of: float | None = None
) -> AdminUsageWindowOut:
    value = float(used) if isinstance(used, int | float) and not isinstance(used, bool) else None
    cap = float(limit) if isinstance(limit, int | float) and not isinstance(limit, bool) else None
    return AdminUsageWindowOut(
        used=value,
        limit=cap,
        fraction=round(value / cap, 4) if value is not None and cap else None,
        resets_at=health.as_datetime(resets_at),
    )


def _api_usage(usage: Mapping[str, Any], settings: Settings) -> AdminApiUsageOut:
    om = usage.get("open_meteo") or {}
    limits, resets = om.get("limits") or {}, om.get("resets_at") or {}
    windy = usage.get("windy") or {}
    flights = usage.get("flights") or {}
    fq = flights.get("quota")
    emails = usage.get("emails") or {}
    return AdminApiUsageOut(
        checked_at=health.as_datetime(usage.get("checked_at")) or utcnow(),
        alert_fraction=settings.quota_alert_fraction,
        open_meteo=AdminOpenMeteoUsageOut(
            **{
                w: _window(om.get(w), limits.get(w), resets.get(w))
                for w in ("minute", "hour", "day", "month")
            },
            available=bool(om.get("available")),
        ),
        windy=AdminWindyUsageOut(
            day=_window(windy.get("day"), windy.get("limit"), windy.get("resets_at")),
            available=bool(windy.get("available")),
            counted=bool(windy.get("counted", True)),
        ),
        flights=AdminFlightUsageOut(
            provider=str(flights.get("provider") or settings.flight_provider),
            today=int(flights.get("today") or 0),
            per_provider={str(k): int(v) for k, v in (flights.get("per_provider") or {}).items()},
            failed_today=int(flights.get("failed_today") or 0),
            quota=(
                _window(fq.get("used"), fq.get("limit"), fq.get("resets_at"))
                if isinstance(fq, Mapping)
                else None
            ),
            user_daily_quota=int(
                flights.get("user_daily_quota") or settings.flight_user_daily_quota
            ),
        ),
        emails=AdminEmailUsageOut(
            provider=str(emails.get("provider") or settings.email_provider),
            today=int(emails.get("today") or 0),
            account=int(emails.get("account") or 0),
            alerts=int(emails.get("alerts") or 0),
            failed=int(emails.get("failed") or 0),
        ),
        requests_24h=[AdminProviderRequestsOut(**r) for r in usage.get("requests_24h") or []],
    )


def _stages(row: SyncRun) -> list[AdminStageOut]:
    out = []
    for entry in row.stages or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        cpu = entry.get("cpu_s")
        try:
            out.append(
                AdminStageOut(
                    name=str(entry["name"]),
                    status=str(entry.get("status") or ""),
                    duration_ms=int(entry.get("duration_ms") or 0),
                    cpu_s=float(cpu) if isinstance(cpu, int | float) else None,
                )
            )
        except (TypeError, ValueError):
            continue
    return out


def _kinds(row: SyncRun) -> list[str]:
    plan = row.plan if isinstance(row.plan, dict) else {}
    kinds = {str(k) for k in plan.get("due_kinds") or []}
    kinds |= {s.name for s in _stages(row) if s.name in health.ACQUISITION_KINDS}
    return sorted(kinds)


def _refresh_jobs(
    syncs: Sequence[SyncRun], history: Sequence[ForecastVersion]
) -> list[AdminRefreshJobOut]:
    by_sync: dict[int, list[int]] = {}
    for v in history:
        if v.sync_run_id is not None:
            by_sync.setdefault(int(v.sync_run_id), []).append(int(v.id))
    out = []
    for row in syncs:
        duration = _gap(row.started_at, row.finished_at)
        out.append(
            AdminRefreshJobOut(
                id=row.id,
                trigger=row.trigger,
                status=row.status,
                refresh=health.is_refresh_sync(row),
                kinds=_kinds(row),
                started_at=row.started_at,
                finished_at=row.finished_at,
                duration_ms=None if duration is None else int(duration * 1000),
                published_at=row.published_at,
                stages=_stages(row),
                version_ids=sorted(by_sync.get(row.id, [])),
                error=redact(row.error_message)[:ERROR_MAX_CHARS] if row.error_message else None,
            )
        )
    return out


def _processing(
    syncs: Sequence[SyncRun], history: Sequence[ForecastVersion]
) -> AdminProcessingTimeOut:
    """Per-stage durations of the finished refresh syncs among ``syncs`` (newest first),
    their total durations, and the versions' pipeline gaps. A sync without a recorded
    stage did no work worth timing (the row migration 0008 writes for the data stored
    before syncs existed starts and ends at the same instant): it is no sample."""
    done = [
        r
        for r in syncs
        if health.is_refresh_sync(r)
        and r.status not in ("running", "skipped")
        and r.finished_at is not None
        and _stages(r)
    ]
    totals = [g * 1000 for r in done if (g := _gap(r.started_at, r.finished_at)) is not None]
    per_stage: dict[str, list[AdminStageOut]] = {}
    for row in done:
        for stage in _stages(row):
            per_stage.setdefault(stage.name, []).append(stage)
    stages = []
    for name, entries in per_stage.items():
        ms = [float(e.duration_ms) for e in entries]
        cpus = [e.cpu_s for e in entries if e.cpu_s is not None]
        stages.append(
            AdminStageTimeOut(
                stage=name,
                runs=len(entries),
                avg_ms=round(sum(ms) / len(ms), 1),
                p95_ms=float(percentile(ms, 0.95) or 0.0),
                max_ms=max(ms),
                avg_cpu_s=round(sum(cpus) / len(cpus), 3) if cpus else None,
            )
        )
    stages.sort(key=lambda s: -s.avg_ms)
    published = [v for v in history if v.status in PUBLISHED_STATUSES and _is_refresh_version(v)]

    def gaps(start: str, end: str) -> list[float]:
        values = [_gap(getattr(v, start), getattr(v, end)) for v in published]
        return [g for g in values if g is not None]

    return AdminProcessingTimeOut(
        refreshes=len(done),
        total_ms=duration_stats(totals),
        stages=stages,
        detected_to_acquired_s=duration_stats(gaps("detected_at", "acquired_at")),
        acquired_to_scored_s=duration_stats(gaps("acquired_at", "scored_at")),
        scored_to_published_s=duration_stats(gaps("scored_at", "published_at")),
    )


def _latency(history: Sequence[ForecastVersion]) -> AdminLatencyOut:
    published = [v for v in history if v.status in PUBLISHED_STATUSES]
    upstream = [
        g
        for v in published
        if _is_refresh_version(v)
        and (g := _gap(v.upstream_available_at, v.published_at)) is not None
    ]
    first_served = []
    for v in published[:FIRST_SERVED_VERSIONS]:
        served_at = versions.first_served_at(int(v.id))
        gap = _gap(v.published_at, served_at)
        if gap is not None:
            first_served.append(gap)
    return AdminLatencyOut(
        upstream_to_publication_s=duration_stats(upstream),
        publication_to_frontend_s=duration_stats(versions.seen_latencies()),
        first_served_s=duration_stats(first_served),
    )


def _scheduler(s: health.SchedulerState, settings: Settings) -> AdminSchedulerOut:
    return AdminSchedulerOut(
        expected_minutes=s.expected_minutes,
        limit_minutes=s.limit_minutes,
        sync_running=s.sync_running,
        enabled=s.enabled,
        refresh_enabled=settings.forecast_refresh_enabled,
        last_check_at=s.last_check_at,
        last_check_by=s.last_check_by,
        checks=[AdminSchedulerCheckOut(source=k, at=v) for k, v in s.checks.items()],
        silent=s.silent,
    )


def _problems(problems: Iterable[health.Problem]) -> list[AdminProblemOut]:
    return [
        AdminProblemOut(
            key=p.key,
            severity=p.severity,
            message=p.message,
            detail=redact(p.detail)[:ERROR_MAX_CHARS] if p.detail else None,
        )
        for p in problems
    ]


# --------------------------------------------------------------------------- report
def freshness_report(
    db: Session, now: datetime | None = None, settings: Settings | None = None
) -> AdminFreshnessOut:
    """The C5 body (see the module docstring)."""
    settings = settings or get_settings()
    now = now or utcnow()
    report = health.data_health(db, now, settings)
    redis_up = report.out.storage.redis is not None and report.out.storage.redis.available
    heartbeat = state.read_heartbeat() if redis_up and not settings.runs_celery else None
    inputs = health.admin_inputs(db, settings, now, heartbeat=heartbeat)
    admin = health.evaluate_admin(inputs, settings)
    current = inputs.current
    codes = set(configured_source_codes(settings))
    if current is not None and current.source_code:
        codes.add(current.source_code)
    detected, acquired, scored = _events(inputs.history, _recent_runs(db, codes))
    notes = list(NOTES)
    if current is None:
        notes.insert(
            0,
            "No forecast version has been published yet: the site serves the newest stored "
            "runs (legacy selection) and the model, spot and latency figures stay empty until "
            "the first versioned refresh.",
        )
    usage = inputs.usage
    return AdminFreshnessOut(
        checked_at=now,
        status=health.overall_status([*report.problems, *admin]),
        version_id=int(current.id) if current is not None else None,
        active_provider=_active_provider(inputs.selection),
        atmosphere=_component(inputs.timings[ATMOSPHERE], current),
        wave=_component(inputs.timings[WAVE], current),
        last_detected_model_update=detected,
        last_successful_acquisition=acquired,
        last_successful_scoring=scored,
        last_published_version=_published(current, now),
        spots_updated=current.spots_updated if current is not None else None,
        stale_spots=[
            AdminStaleSpotOut(
                spot_id=s.spot_id,
                slug=s.slug,
                name=s.name,
                last_updated_at=s.last_updated_at,
                reason=s.reason,
                error=redact(s.error)[:ERROR_MAX_CHARS] if s.error else None,
            )
            for s in inputs.stale
        ],
        failed_requests=[
            AdminFailedRequestOut(**r) for r in usage.get("failed_requests_24h") or []
        ],
        api_usage=_api_usage(usage, settings),
        processing_time=_processing(inputs.syncs, inputs.history),
        latency=_latency(inputs.history),
        refresh_jobs=_refresh_jobs(inputs.syncs, inputs.history),
        versions=_version_rows(inputs.history),
        scheduler=_scheduler(inputs.scheduler, settings),
        problems=_problems(admin),
        health_problems=_problems(report.problems),
        notes=notes,
    )
