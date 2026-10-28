"""The admin problems of design D12 (app.services.health) and the dashboard's statistics
(app.services.admin_freshness), without a database.

Versions, syncs and counters below are SYNTHETIC test inputs; the lags are modelled on the
Open-Meteo GFS delays measured on 2026-10-26 (about 317 min for GFS-Wave).
"""

from __future__ import annotations

import types
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis

from app.core.config import Settings, get_settings
from app.core.redis import get_redis
from app.models.sync import SyncRun
from app.models.version import ForecastVersion
from app.schemas.health import (
    DataHealthOut,
    HeartbeatOut,
    MapFieldsHealthOut,
    ObservationsHealthOut,
    StorageOut,
)
from app.services import admin_freshness, health, usage
from app.services.forecasts import quota, versions
from app.services.forecasts.base import ATMOSPHERE, WAVE
from app.services.forecasts.versions import ComponentRun
from app.services.notifications.ops_alerts import plan_alerts, render_alert
from app.services.sync.state import Heartbeat

NOW = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
LAG = timedelta(minutes=317)


def settings(**overrides: Any) -> Settings:
    return get_settings().model_copy(update=overrides)


def version(
    vid: int,
    init: datetime,
    *,
    lag: timedelta = LAG,
    status: str = "published",
    interval: int | None = 21600,
    detected: datetime | None = None,
    wave_changed: bool = True,
    fallback_from: str | None = None,
) -> ForecastVersion:
    comps = {
        WAVE: ComponentRun(
            WAVE,
            model="ncep_gfswave025",
            init_time=init,
            available_at=init + lag,
            retrieved_at=init + lag + timedelta(minutes=12),
            changed=wave_changed,
            update_interval_seconds=interval,
        ).as_dict(),
        ATMOSPHERE: ComponentRun(
            ATMOSPHERE,
            model="ncep_gfs013",
            init_time=init,
            available_at=init + lag + timedelta(minutes=5),
            retrieved_at=init + lag + timedelta(minutes=12),
            update_interval_seconds=interval,
        ).as_dict(),
    }
    return ForecastVersion(
        id=vid,
        status=status,
        provider="open_meteo",
        source_code="open_meteo_noaa",
        components=comps,
        spot_runs={},
        detected_at=detected or init + lag + timedelta(minutes=10),
        fallback_from=fallback_from,
        spots_total=50,
        timings={"kind": "refresh"},
    )


# --------------------------------------------------------------------------- model_run_late
def test_the_expected_availability_is_the_measured_lag_after_the_next_cycle() -> None:
    c = NOW.replace(hour=6)
    history = [version(3, c), version(2, c - timedelta(hours=6), lag=LAG + timedelta(minutes=40))]
    timings = health.component_timings(history, history[0], NOW, settings())
    wave = timings[WAVE]
    assert wave.lag_samples == 2
    assert wave.typical_lag_s == pytest.approx((317 + 20) * 60)  # median of 317 and 357 min
    assert wave.interval_recorded and wave.interval_s == 21600
    assert wave.expected_next_at == c + timedelta(hours=6) + timedelta(minutes=337)
    # Grace: the measured spread (20 min) is below MODEL_RUN_LATE_GRACE, so 90 min.
    assert wave.late_after == wave.expected_next_at + health.MODEL_RUN_LATE_GRACE
    assert not wave.late  # 18:00 is before 12:00 + 5.6 h + 1.5 h


def test_a_run_past_its_expected_availability_plus_grace_is_late() -> None:
    c = NOW.replace(hour=0)
    history = [version(2, c)]
    s = settings()
    late_at = c + timedelta(hours=6) + LAG + health.MODEL_RUN_LATE_GRACE
    early = health.component_timings(history, history[0], late_at - timedelta(minutes=1), s)
    assert health.model_run_late_problems(early, late_at - timedelta(minutes=1), s) == []
    after = late_at + timedelta(minutes=1)
    problems = health.model_run_late_problems(
        health.component_timings(history, history[0], after, s), after, s
    )
    assert [p.key for p in problems] == ["model_run_late:wave"]  # the wind lags 5 min more
    after = late_at + timedelta(minutes=6)
    problems = health.model_run_late_problems(
        health.component_timings(history, history[0], after, s), after, s
    )
    assert [p.key for p in problems] == ["model_run_late:atmosphere", "model_run_late:wave"]
    text = problems[1].message
    assert "2026-10-28 06:00 UTC" in text and "median of 1 run" in text
    assert "meta.json update_interval_seconds" in text
    # A newer run seen by a refresh (even unpublished) is not late: no problem.
    seen = health.component_timings(history, history[0], after, s, {WAVE: c + timedelta(hours=6)})
    assert not seen[WAVE].late and seen[ATMOSPHERE].late


def test_a_wide_measured_spread_widens_the_grace_and_no_lag_means_no_judgement() -> None:
    c = NOW.replace(hour=0)
    spread = [
        version(3, c),
        version(2, c - timedelta(hours=6)),
        version(1, c - timedelta(hours=12), lag=LAG + timedelta(hours=3)),
    ]
    wave = health.component_timings(spread, spread[0], NOW, settings())[WAVE]
    assert wave.lag_spread_s == pytest.approx(3 * 3600)
    assert wave.late_after == wave.expected_next_at + timedelta(hours=3)  # type: ignore[operator]
    # Without an availability time there is nothing measured: never a wall-clock guess.
    blind = version(1, c - timedelta(days=2))
    for comp in blind.components.values():
        comp["available_at"] = None
    timings = health.component_timings([blind], blind, NOW, settings())
    assert timings[WAVE].typical_lag_s is None and not timings[WAVE].late
    assert health.model_run_late_problems(timings, NOW, settings()) == []


def test_without_a_recorded_interval_six_hours_are_assumed_and_said_so() -> None:
    c = NOW - timedelta(days=1)
    history = [version(1, c.replace(minute=0), interval=None)]
    timings = health.component_timings(history, history[0], NOW, settings())
    assert timings[WAVE].interval_s == 6 * 3600 and not timings[WAVE].interval_recorded
    problems = health.model_run_late_problems(timings, NOW, settings())
    assert problems and "assumed" in problems[0].message
    assert (
        health.model_run_late_problems(timings, NOW, settings(forecast_refresh_enabled=False)) == []
    )


def test_a_run_mixing_domain_cycles_is_no_lag_sample() -> None:
    """A component refreshes as soon as one domain advances: GFS 0.13 of 06Z with GFS 0.25
    still at 00Z is such a run. Its init time is the older cycle and its availability the
    newer domain's, so as a lag sample it would add a whole cycle (6 h) to the lag."""
    c0 = NOW.replace(hour=0) - timedelta(days=1)
    c6 = c0 + timedelta(hours=6)
    lag = timedelta(minutes=322)

    def atmosphere(vid: int, domains: list[tuple[str, datetime, datetime]]) -> ForecastVersion:
        doms = tuple(versions.DomainRun(m, init, avail) for m, init, avail in domains)
        run = ComponentRun(
            ATMOSPHERE,
            model="ncep_gfs013,ncep_gfs025",
            init_time=min(d.init_time for d in doms if d.init_time),
            available_at=max(d.available_at for d in doms if d.available_at),
            update_interval_seconds=21600,
            domains=doms,
        )
        return ForecastVersion(
            id=vid, status="superseded", components={ATMOSPHERE: run.as_dict()}, timings={}
        )

    pure0 = atmosphere(1, [("ncep_gfs013", c0, c0 + lag - LAG / 8), ("ncep_gfs025", c0, c0 + lag)])
    mixed = atmosphere(2, [("ncep_gfs013", c6, c6 + lag - LAG / 8), ("ncep_gfs025", c0, c0 + lag)])
    pure6 = atmosphere(3, [("ncep_gfs013", c6, c6 + lag - LAG / 8), ("ncep_gfs025", c6, c6 + lag)])
    assert mixed.components[ATMOSPHERE]["init_time"] == c0.isoformat()  # the older domain
    wind = health.component_timings([pure6, mixed, pure0], pure6, NOW, settings())[ATMOSPHERE]
    assert wind.lag_samples == 2 and wind.typical_lag_s == pytest.approx(lag.total_seconds())
    assert wind.expected_next_at == c6 + timedelta(hours=6) + lag
    # Only the mixed run known for c0: no sample from it at all.
    alone = health.component_timings([mixed], mixed, NOW, settings())[ATMOSPHERE]
    assert alone.lag_samples == 0 and alone.typical_lag_s is None and not alone.late


def test_times_without_an_offset_are_read_as_utc() -> None:
    assert health.as_datetime("2026-10-28T10:00:00") == datetime(2026, 10, 28, 10, tzinfo=UTC)
    assert health.as_datetime(datetime(2026, 10, 28, 10)) == datetime(2026, 10, 28, 10, tzinfo=UTC)
    assert health.as_datetime("2026-10-28T10:00:00+02:00") == datetime(2026, 10, 28, 8, tzinfo=UTC)
    assert health.as_datetime("soon") is None and health.as_datetime(None) is None
    # A record without offsets compares with the aware clock instead of raising.
    record = {"windy": {"last_failure_kind": "credentials", "last_failure_at": "2026-10-28T10:00"}}
    assert [f.at for f in health._health_failures(record)] == [
        datetime(2026, 10, 28, 10, tzinfo=UTC)
    ]


def test_daemon_detection_counts_for_component_less_entries() -> None:
    beat = Heartbeat(
        at=NOW,
        runner="daemon",
        due=False,
        components=[
            {
                "kind": "spots",
                "source_code": "open_meteo_noaa",
                "available_cycle": "2026-10-28T12:00:00+00:00",
            },
            {
                "kind": "map",
                "source_code": "noaa_gfs_fields",
                "available_cycle": "2026-10-28T18:00:00+00:00",
            },
            {"kind": "spots", "component": "wave", "available_cycle": "2026-10-28T18:00:00+00:00"},
        ],
    )
    seen = health._detected_runs(beat, NOW)
    assert seen == {
        ATMOSPHERE: datetime(2026, 10, 28, 12, tzinfo=UTC),
        WAVE: datetime(2026, 10, 28, 18, tzinfo=UTC),
    }
    old = Heartbeat(
        at=NOW - timedelta(hours=7), runner="daemon", due=False, components=beat.components
    )
    assert health._detected_runs(old, NOW) == {}


# --------------------------------------------------------------------------- refresh_failures
def sync(
    sid: int, status: str, *, trigger: str = "http", kinds: Sequence[str] = ("spots",)
) -> SyncRun:
    return SyncRun(
        id=sid,
        trigger=trigger,
        status=status,
        started_at=NOW - timedelta(minutes=100 - sid),
        plan={"due_kinds": list(kinds)},
        stages=[{"name": k, "status": "success", "duration_ms": 10} for k in kinds],
        error_message=f"error of sync {sid}",
    )


def test_three_failed_refreshes_in_a_row_make_the_problem() -> None:
    two = [sync(5, "failed"), sync(4, "abandoned"), sync(3, "success")]
    assert health.refresh_failures_problem(two) is None
    three = [
        sync(7, "running"),
        sync(6, "skipped"),
        sync(5, "failed", trigger="daemon", kinds=("map",)),  # map-only: ignored
        sync(4, "failed"),
        sync(3, "failed", trigger="cli"),
        sync(2, "abandoned"),
        sync(1, "success"),
    ]
    problem = health.refresh_failures_problem(three)
    assert problem is not None and problem.key == "refresh_failures"
    assert "The last 3 forecast refreshes failed (syncs 2-4" in problem.message
    assert problem.detail == "error of sync 4"  # the newest refresh's error, admins only
    every = [sync(i, "failed") for i in range(health.REFRESH_HISTORY, 0, -1)]
    assert "The last 20+ forecast" in health.refresh_failures_problem(every).message  # type: ignore[union-attr]


def test_what_counts_as_a_refresh_sync() -> None:
    assert health.is_refresh_sync(sync(1, "failed", trigger="http", kinds=("map",)))
    assert not health.is_refresh_sync(
        sync(1, "failed", trigger="daemon", kinds=("map", "ensemble"))
    )
    assert health.is_refresh_sync(sync(1, "failed", trigger="daemon", kinds=("spots", "map")))
    bare = SyncRun(id=1, trigger="cli", status="failed", started_at=NOW, plan={}, stages=[])
    assert health.is_refresh_sync(bare)  # failed before planning: counted


# --------------------------------------------------------------------------- providers
def test_a_refused_key_fails_unless_a_fallback_stands_in() -> None:
    failures = [
        health.CredentialFailure(
            "windy", "windy_gfswave", NOW - timedelta(hours=1), "credentials", "HTTP 401"
        ),
        health.CredentialFailure("windy", None, NOW, "testing_key", "trial data"),
        health.CredentialFailure("open_meteo", "open_meteo_noaa", NOW, "credentials", "HTTP 403"),
    ]
    problems = health.credential_problems(
        failures, {"fallback": "open_meteo", "fallback_from": "windy"}
    )
    by_key = {p.key: p for p in problems}
    assert by_key["provider_credentials:open_meteo"].severity == "failing"
    windy = by_key["provider_credentials:windy"]
    assert windy.severity == "degraded" and "TESTING key" in windy.message  # the newest
    assert "open_meteo stands in meanwhile" in windy.message
    alone = health.credential_problems(failures[:1], {"fallback": None, "fallback_from": None})
    assert alone[0].severity == "failing"


def test_recorded_credential_failures_clear_with_a_later_success() -> None:
    health_rows = {
        "windy": {
            "last_failure_kind": "credentials",
            "last_failure_at": "2026-10-28T10:00:00+00:00",
            "last_success_at": "2026-10-28T11:00:00+00:00",
        },
        "other": {
            "last_failure_kind": "testing_key",
            "last_failure_at": "2026-10-28T10:00:00+00:00",
            "last_failure_detail": "trial answer for api_key=abcdef0123456789abcdef",
        },
        "quiet": {
            "last_failure_kind": "unavailable",
            "last_failure_at": "2026-10-28T10:00:00+00:00",
        },
    }
    (failure,) = health._health_failures(health_rows)
    assert (failure.provider, failure.kind) == ("other", "testing_key")
    # Another package's record: its text is redacted before it reaches an email or the page.
    assert failure.detail is not None and "abcdef0123456789abcdef" not in failure.detail


def test_metadata_observations_take_the_newest_check() -> None:
    beat = Heartbeat(
        at=NOW - timedelta(minutes=10),
        runner="daemon",
        due=False,
        components=[
            {"source_code": "open_meteo_noaa", "state": "unknown", "detail": "ReadTimeout"},
            {"source_code": "noaa_gfs_fields", "state": "current"},
            {"source_code": "noaa_gefs_wave", "state": "disabled"},
        ],
    )
    refresh = {
        "at": NOW.isoformat(),
        "components": [{"source_code": "open_meteo_noaa", "component": "wave", "state": "new"}],
    }
    windy = {"windy": {"last_failure_kind": "unavailable", "last_failure_at": NOW.isoformat()}}
    seen = health.metadata_observations(beat, refresh, windy, NOW)
    assert seen["open_meteo_noaa"].unknown and seen["open_meteo_noaa"].detail == "ReadTimeout"
    assert not seen["open_meteo_noaa:wave"].unknown
    assert not seen["noaa_gfs_fields"].unknown and "noaa_gefs_wave" not in seen
    assert seen["windy"].unknown
    state = {
        "open_meteo_noaa": health.MetadataState(
            "open_meteo_noaa", True, NOW, NOW - timedelta(minutes=31), "ReadTimeout"
        ),
        "windy": health.MetadataState("windy", True, NOW, NOW - timedelta(minutes=29)),
    }
    problems = health.provider_unavailable_problems(
        state, NOW, {"open_meteo_noaa": "Open-Meteo GFS"}
    )
    assert [p.key for p in problems] == ["provider_unavailable:open_meteo_noaa"]
    assert problems[0].message.startswith("Open-Meteo GFS: the provider's run metadata")


def test_an_active_fallback_from_the_manager_or_the_published_version() -> None:
    selection = {
        "fallback": "open_meteo",
        "fallback_from": "windy",
        "fallback_until": "2026-10-28T19:00:00+00:00",
        "reasons": ["Windy failed (credentials): open_meteo stands in until 19:00 UTC"],
    }
    (problem,) = health.provider_fallback_problems(selection, None)
    assert problem.key == "provider_fallback:windy"
    assert (
        "until 2026-10-28 19:00 UTC" in problem.message and "stands in for windy" in problem.message
    )
    published = version(4, NOW.replace(hour=6), fallback_from="windy")
    (from_version,) = health.provider_fallback_problems({"fallback_from": None}, published)
    assert "published version 4 came from open_meteo" in from_version.message
    assert health.provider_fallback_problems({"fallback_from": None}, version(5, NOW)) == []


# --------------------------------------------------------------------------- quota_near
def test_quota_near_at_the_alert_fraction_of_each_counter() -> None:
    counters: dict[str, Any] = {
        "open_meteo": {
            "day": 7999.0,
            "month": 240000.0,
            "limits": {"day": 10000, "month": 300000},
            "resets_at": {"day": "2026-10-29T00:00:00+00:00", "month": "2026-11-01T00:00:00+00:00"},
        },
        "windy": {"day": 9000, "limit": 10000, "counted": True, "resets_at": None},
        "flights": {"provider": "duffel", "quota": {"used": 100, "limit": 300}},
    }
    keys = [p.key for p in health.quota_near_problems(counters, settings())]
    assert keys == ["quota_near:open_meteo_month", "quota_near:windy_day"]
    keys = [p.key for p in health.quota_near_problems(counters, settings(quota_alert_fraction=0.3))]
    assert keys == [
        "quota_near:open_meteo_day",
        "quota_near:open_meteo_month",
        "quota_near:windy_day",
        "quota_near:flights_duffel_day",
    ]
    # Not counted (no Windy counter in this build), unreadable (Redis down), no quota (demo).
    blind = {
        "open_meteo": {"day": None, "month": None, "limits": {"day": 1, "month": 1}},
        "windy": {"day": 9999, "limit": 10000, "counted": False},
        "flights": {"provider": "demo", "quota": None},
    }
    assert health.quota_near_problems(blind, settings()) == []


# --------------------------------------------------------------------------- scheduler
def scheduler(**kw: Any) -> health.SchedulerState:
    base: dict[str, Any] = {
        "expected_minutes": 30,
        "enabled": True,
        "last_check_at": None,
        "last_check_by": None,
        "checks": {},
        "monitoring_since": NOW - timedelta(hours=2),
        "silent": True,
    }
    return health.SchedulerState(**(base | kw))


def test_scheduler_silence_messages() -> None:
    never = health.scheduler_silent_problem(scheduler(), NOW)
    assert never is not None and "No forecast refresh check has been recorded" in never.message
    last = health.scheduler_silent_problem(
        scheduler(last_check_at=NOW - timedelta(minutes=45), last_check_by="daemon"), NOW
    )
    assert last is not None and "(daemon), 45 min ago" in last.message
    assert "REFRESH_SCHEDULER_EXPECTED_MINUTES=0" in last.message
    assert health.scheduler_silent_problem(scheduler(silent=False), NOW) is None


# --------------------------------------------------------------------------- providers
def test_forecast_usage_is_the_providers_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = {
        "checked_at": NOW.isoformat(),
        "open_meteo": {"day": 12.0, "limits": {"day": 10000}, "available": True},
        "windy": {"day": 104, "limit": 10000, "available": True},
    }
    monkeypatch.setattr(quota, "usage_snapshot", lambda now, s: snapshot)
    out = usage.forecast_usage(NOW, settings())
    assert out["open_meteo"]["day"] == 12.0 and out["windy"]["day"] == 104
    assert out["windy"]["counted"] is True


def test_forecast_usage_counts_windy_requests_from_redis() -> None:
    """The Windy meter of the dashboard reads the adapter's daily counter (counted)."""
    s = settings(windy_daily_request_limit=500)
    try:
        get_redis().delete(quota.windy_day_window(NOW, s).key)
        quota.consume_windy_request(now=NOW, settings=s)
    except redis.RedisError:
        pytest.skip("Redis is not reachable")
    windy = usage.forecast_usage(NOW, s)["windy"]
    assert windy["counted"] is True and windy["available"] is True
    assert windy["day"] == 1 and windy["limit"] == 500


def test_provider_selection_uses_the_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.forecasts import provider_manager

    active = types.SimpleNamespace(
        as_dict=lambda now: {"providers": ["open_meteo"], "fallback_from": "windy", "health": {}}
    )
    monkeypatch.setattr(provider_manager, "active_provider", lambda s, now: active)
    assert health.provider_selection_state(settings(), NOW)["fallback_from"] == "windy"

    def unreadable(s: object, now: object) -> object:
        raise redis.ConnectionError("Redis down")

    # When the manager cannot be read, the settings selection (no fallback recorded).
    monkeypatch.setattr(provider_manager, "active_provider", unreadable)
    plain = health.provider_selection_state(settings(), NOW)
    assert plain["fallback_from"] is None and plain["providers"] == ["demo"]
    assert plain["configured"] == ["demo"] and plain["health"] == {}


# --------------------------------------------------------------------------- alerts
def report(public: list[health.Problem], admin: list[health.Problem]) -> health.HealthReport:
    out = DataHealthOut(
        status=health.overall_status(public),
        checked_at=NOW,
        reasons=[p.message for p in public],
        problems=[],
        forecast_sources=[],
        jobs=[],
        map_fields=MapFieldsHealthOut(mode="off", status="off"),
        observations=ObservationsHealthOut(enabled=False),
        heartbeat=HeartbeatOut(limit_minutes=30),
        storage=StorageOut(database_bytes=0),
    )
    return health.HealthReport(out=out, problems=public, admin_problems=admin)


def test_admin_problems_join_the_alerts_but_never_the_public_report() -> None:
    admin = [health.Problem("provider_credentials:open_meteo", "failing", "Key refused.")]
    public = [health.Problem("forecast_stale:demo", "degraded", "Demo is old.")]
    r = report(public, admin)
    assert r.status == "degraded" and r.alert_status == "failing"
    assert [p.key for p in r.all_problems] == [
        "provider_credentials:open_meteo",
        "forecast_stale:demo",
    ]
    assert r.out.problems == [] and "Key refused." not in r.out.reasons
    repeat = timedelta(hours=12)
    plan = plan_alerts({}, r.all_problems, NOW, repeat)
    subject, text = render_alert(r, plan, get_settings(), repeat)
    assert "Data health FAILING: 2 problems" in subject
    assert "/admin/freshness" in text and "Key refused." in text


# --------------------------------------------------------------------------- statistics
def test_percentiles_and_duration_stats() -> None:
    assert admin_freshness.percentile([], 0.5) is None
    assert admin_freshness.percentile([3.0, 1.0, 2.0], 0.5) == 2.0
    assert admin_freshness.percentile(list(map(float, range(1, 21))), 0.95) == 19.0
    stats = admin_freshness.duration_stats([30.0, 10.0, 20.0])
    assert stats.samples == 3 and stats.last == 30.0 and stats.avg == 20.0
    assert stats.p50 == 20.0 and stats.p95 == 30.0 and stats.max == 30.0
    assert admin_freshness.duration_stats([]).samples == 0


def test_a_sync_without_stages_is_no_processing_time_sample() -> None:
    """Migration 0008 records the data stored before syncs existed as one 'pipeline' sync
    with no stage that starts and ends at the same instant: not a 0 ms refresh."""
    timed = sync(2, "success")
    timed.finished_at = timed.started_at + timedelta(seconds=12)
    legacy = SyncRun(
        id=1, trigger="pipeline", status="success", started_at=NOW, finished_at=NOW, plan={}
    )
    legacy.stages = []
    out = admin_freshness._processing([timed, legacy], [])
    assert out.refreshes == 1 and out.total_ms.samples == 1 and out.total_ms.last == 12000.0


def test_a_retry_version_is_not_an_upstream_latency_sample() -> None:
    v = version(1, NOW.replace(hour=6))
    assert admin_freshness._is_refresh_version(v)
    v.timings = {"kind": "retry"}
    assert not admin_freshness._is_refresh_version(v)
    carried = version(2, NOW.replace(hour=6), wave_changed=False)
    assert admin_freshness._changed(carried) == ["atmosphere"]
