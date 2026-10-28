"""The admin problems of design D12 through the ops.data_health job, against PostgreSQL and
Redis: each one is raised by the state it describes and recovered when that state clears,
emailed to verified admins only (never to users), de-duplicated like every other problem,
and the scheduler-silence check is off at REFRESH_SCHEDULER_EXPECTED_MINUTES=0.

Every row is SYNTHETIC (tests/admin_fixtures.py); no provider is called.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.core.redis import consume_quota, get_redis
from app.jobs import runner
from app.models import BackgroundJobLog
from app.models.enums import ForecastRunStatus, JobStatus
from app.models.sync import SyncRun
from app.services import health
from app.services.forecasts import quota
from app.services.health import METADATA_UNKNOWN_KEY, data_health, expected_jobs
from app.services.notifications.ops_alerts import run_data_health_check
from app.services.sync import state as sync_state
from app.services.sync.types import SYNC_TICK_JOB
from app.workers import tasks
from app.workers.schedule import DATA_HEALTH_TASK
from tests import admin_fixtures as fx
from tests.conftest import outbox_messages

pytestmark = pytest.mark.integration

ADMIN = "ops-admin@example.com"


@pytest.fixture
def people(db: Session) -> None:
    """One verified admin; a verified user, an unverified admin and an inactive admin, who
    must never get these emails."""
    fx.make_user(db, ADMIN, admin=True)
    fx.make_user(db, "surfer@example.com", admin=False)
    fx.make_user(db, "unverified-admin@example.com", admin=True, verified=False)
    fx.make_user(db, "former-admin@example.com", admin=True, active=False)


@pytest.fixture
def open_meteo(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(get_settings(), "forecast_providers", "open_meteo")
    yield


def ops_emails() -> list[dict[str, object]]:
    return [m for m in outbox_messages() if m.get("tag") == "ops_alert"]


def log(db: Session, name: str, started: datetime) -> None:
    db.add(
        BackgroundJobLog(
            job_name=name,
            status=JobStatus.SUCCESS,
            started_at=started,
            finished_at=started + timedelta(seconds=5),
            duration_ms=5000,
            details={},
        )
    )
    db.commit()


def check(
    db: Session, monkeypatch: pytest.MonkeyPatch, now: datetime, *, tick: datetime | None = None
) -> dict[str, object]:
    """One ops.data_health run at ``now`` through the job runner (its alert state persists),
    with every other scheduled job logged as successful at ``now`` (``tick``: when Celery's
    sync.tick last ran, default ``now``)."""
    for job in expected_jobs(get_settings()):
        if job.name == DATA_HEALTH_TASK:
            continue
        log(db, job.name, (tick or now) if job.name == SYNC_TICK_JOB else now)
    monkeypatch.setattr(runner, "utcnow", lambda: now)
    return tasks.run_job(DATA_HEALTH_TASK, lambda s: run_data_health_check(s, now=now), chain=False)


def alerts(result: dict[str, object], prefix: str) -> list[str]:
    return [a for a in result["alerts"] if a.split(":", 1)[1].startswith(prefix)]  # type: ignore[union-attr]


def only_admins_were_emailed() -> None:
    sent = ops_emails()
    assert sent, "no ops alert was sent"
    assert {m["to"] for m in sent} == {ADMIN}


# --------------------------------------------------------------------------- the problems
def test_a_late_model_run_is_alerted_then_recovered(
    db: Session, people: None, open_meteo: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = utcnow()
    c1 = fx.cycle_of(t0) - timedelta(hours=18)
    run1 = fx.forecast_run(db, c1, f"om:{c1:%Y%m%d%H}", started=c1 + timedelta(hours=5, minutes=30))
    fx.publish(
        db, run1, [fx.wave(c1), fx.wind(c1)], published_at=c1 + timedelta(hours=5, minutes=35)
    )

    first = check(db, monkeypatch, t0)
    assert alerts(first, "model_run_late") == [
        "new:model_run_late:atmosphere",
        "new:model_run_late:wave",
    ]
    only_admins_were_emailed()
    (mail,) = ops_emails()
    assert "is late" in str(mail["text"]) and "median of 1 run" in str(mail["text"])
    assert "/admin/freshness" in str(mail["text"])

    # A newer run of both components arrives and is published: both recover.
    c2 = c1 + timedelta(hours=12)
    run2 = fx.forecast_run(db, c2, f"om:{c2:%Y%m%d%H}", started=t0)
    fx.publish(db, run2, [fx.wave(c2), fx.wind(c2)], published_at=t0 + timedelta(seconds=30))
    later = check(db, monkeypatch, t0 + timedelta(minutes=15))
    assert sorted(alerts(later, "model_run_late")) == [
        "recovered:model_run_late:atmosphere",
        "recovered:model_run_late:wave",
    ]
    only_admins_were_emailed()


def test_stale_spots_are_alerted_then_recovered(
    db: Session, people: None, open_meteo: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = utcnow()
    c = fx.cycle_of(t0 - fx.WIND_LAG - timedelta(minutes=20))
    spots = fx.active_spot_ids(db)
    run = fx.forecast_run(db, c, f"om:{c:%Y%m%d%H}", started=t0 - timedelta(minutes=5))
    first_version = fx.publish(
        db, run, [fx.wave(c), fx.wind(c)], published_at=t0 - timedelta(minutes=4)
    )
    assert first_version.spots_stale == 0
    retry = fx.forecast_run(db, c, f"om:{c:%Y%m%d%H}#partial", started=t0 - timedelta(minutes=3))
    fx.publish(
        db,
        retry,
        [fx.wave(c), fx.wind(c)],
        published_at=t0 - timedelta(minutes=2),
        stale={spots[0]: "HTTP 500", spots[1]: "masked cells", spots[2]: "timeout"},
        updated=spots[3:],
    )
    first = check(db, monkeypatch, t0)
    assert alerts(first, "forecast_stale_spots") == ["new:forecast_stale_spots"]
    text = str(ops_emails()[0]["text"])
    assert "3 of " in text and "refresh failed" in text and "last error:" in text
    # Unchanged 15 minutes later: de-duplicated.
    assert alerts(check(db, monkeypatch, t0 + timedelta(minutes=15)), "forecast_stale") == []
    assert len(ops_emails()) == 1

    fix = fx.forecast_run(db, c, f"om:{c:%Y%m%d%H}#retry", started=t0 + timedelta(minutes=20))
    fx.publish(
        db,
        fix,
        [fx.wave(c), fx.wind(c)],
        published_at=t0 + timedelta(minutes=21),
        updated=spots[:3],
        kind="retry",
    )
    later = check(db, monkeypatch, t0 + timedelta(minutes=30))
    assert alerts(later, "forecast_stale_spots") == ["recovered:forecast_stale_spots"]
    only_admins_were_emailed()


def test_three_failed_refreshes_in_a_row_are_alerted_then_recovered(
    db: Session, people: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = utcnow()
    fx.sync_run(db, t0 - timedelta(hours=3))  # the last good one
    for minutes in (50, 35):
        fx.sync_run(db, t0 - timedelta(minutes=minutes), status="failed", error="HTTP 500")
    two = check(db, monkeypatch, t0)
    assert alerts(two, "refresh_failures") == []  # two in a row are not enough
    fx.sync_run(
        db,
        t0 - timedelta(minutes=20),
        trigger="daemon",
        kinds=("map",),
        stages=(("map", 38600, 30.9),),
        status="failed",
    )  # a map-only sync does not count either way
    fx.sync_run(db, t0 - timedelta(minutes=5), status="failed", error="db-host-7 refused")
    three = check(db, monkeypatch, t0 + timedelta(minutes=1))
    assert alerts(three, "refresh_failures") == ["new:refresh_failures"]
    mail = str(ops_emails()[0]["text"])
    assert "The last 3 forecast refreshes failed" in mail
    assert "last error: db-host-7 refused" in mail  # the error goes to admins only
    report = data_health(db, now=t0 + timedelta(minutes=1)).out.model_dump_json()
    assert "db-host-7" not in report and "refresh_failures" not in report  # never public
    with_admin = data_health(db, now=t0 + timedelta(minutes=1), admin=True)
    assert "refresh_failures" in {p.key for p in with_admin.admin_problems}
    assert "refresh_failures" not in with_admin.out.model_dump_json()  # out stays public

    fx.sync_run(db, t0 + timedelta(minutes=10))
    later = check(db, monkeypatch, t0 + timedelta(minutes=15))
    assert alerts(later, "refresh_failures") == ["recovered:refresh_failures"]
    only_admins_were_emailed()


def test_a_refused_key_is_alerted_then_recovered(
    db: Session, people: None, open_meteo: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = utcnow()
    c = fx.cycle_of(t0 - fx.WIND_LAG - timedelta(minutes=20))
    fx.forecast_run(
        db,
        c,
        f"om:{c:%Y%m%d%H}#refused",
        status=ForecastRunStatus.FAILED,
        stats={"requests": 1, "http_status_counts": {"401": 1}},
        error="Open-Meteo: API key invalid",
        started=t0 - timedelta(minutes=10),
    )
    first = check(db, monkeypatch, t0)
    assert alerts(first, "provider_credentials") == ["new:provider_credentials:open_meteo"]
    # Failing: no fallback stands in for Open-Meteo.
    assert "[failing] Forecast provider open_meteo refused the API key" in str(
        ops_emails()[0]["text"]
    )

    fx.forecast_run(db, c, f"om:{c:%Y%m%d%H}", started=t0 + timedelta(minutes=5))
    later = check(db, monkeypatch, t0 + timedelta(minutes=15))
    assert alerts(later, "provider_credentials") == ["recovered:provider_credentials:open_meteo"]
    only_admins_were_emailed()


def heartbeat(at: datetime, state: str, detail: str | None = None) -> None:
    """The daemon's heartbeat (app.services.sync.state) with one spot component."""
    component = {
        "kind": "spots",
        "source_code": fx.SOURCE,
        "provider": "open_meteo",
        "state": state,
        "stored_run_key": None,
        "available_run_key": None,
        "available_cycle": None,
        "detail": detail,
    }
    payload = {"at": at.isoformat(), "runner": "daemon", "due": False, "components": [component]}
    get_redis().set(sync_state.HEARTBEAT_KEY, json.dumps(payload), ex=3600)


def test_unreadable_metadata_for_30_minutes_is_alerted_then_recovered(
    db: Session, people: None, open_meteo: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "job_runner", "daemon")
    t0 = utcnow()
    heartbeat(t0, "unknown", "ReadTimeout reading meta.json")
    assert alerts(check(db, monkeypatch, t0), "provider_unavailable") == []  # just now
    assert get_redis().hget(METADATA_UNKNOWN_KEY, fx.SOURCE) == t0.isoformat()
    heartbeat(t0 + timedelta(minutes=20), "unknown", "ReadTimeout reading meta.json")
    assert alerts(check(db, monkeypatch, t0 + timedelta(minutes=20)), "provider_unav") == []
    heartbeat(t0 + timedelta(minutes=35), "unknown", "ReadTimeout reading meta.json")
    raised = check(db, monkeypatch, t0 + timedelta(minutes=35))
    assert alerts(raised, "provider_unavailable") == [f"new:provider_unavailable:{fx.SOURCE}"]
    assert "could not be read since" in str(ops_emails()[0]["text"])

    heartbeat(t0 + timedelta(minutes=45), "current")
    later = check(db, monkeypatch, t0 + timedelta(minutes=45))
    assert alerts(later, "provider_unavailable") == [f"recovered:provider_unavailable:{fx.SOURCE}"]
    assert get_redis().hget(METADATA_UNKNOWN_KEY, fx.SOURCE) is None
    only_admins_were_emailed()


def test_an_active_fallback_is_alerted_then_recovered(
    db: Session, people: None, open_meteo: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = utcnow()
    c = fx.cycle_of(t0 - fx.WIND_LAG - timedelta(minutes=20))
    run = fx.forecast_run(db, c, f"om:{c:%Y%m%d%H}", started=t0 - timedelta(minutes=5))
    fx.publish(
        db,
        run,
        [fx.wave(c), fx.wind(c)],
        published_at=t0 - timedelta(minutes=4),
        fallback_from="windy",
    )
    first = check(db, monkeypatch, t0)
    assert alerts(first, "provider_fallback") == ["new:provider_fallback:windy"]
    assert "stands in for windy" in str(ops_emails()[0]["text"])

    primary = fx.forecast_run(db, c, f"om:{c:%Y%m%d%H}#2", started=t0 + timedelta(minutes=5))
    fx.publish(db, primary, [fx.wave(c), fx.wind(c)], published_at=t0 + timedelta(minutes=6))
    later = check(db, monkeypatch, t0 + timedelta(minutes=15))
    assert alerts(later, "provider_fallback") == ["recovered:provider_fallback:windy"]
    only_admins_were_emailed()


def test_quotas_near_their_limits_are_alerted_then_recovered(
    db: Session, people: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "open_meteo_calls_per_day", 100)
    monkeypatch.setattr(settings, "flight_provider", "duffel")
    t0 = utcnow()
    quota.consume_open_meteo_budget(79.0, now=t0)
    consume_quota("duffel", settings.flight_daily_quota, 239)  # 79.7 % of 300
    below = check(db, monkeypatch, t0)
    assert alerts(below, "quota_near") == []
    quota.consume_open_meteo_budget(6.0, now=t0)  # 85 % of the day
    consume_quota("duffel", settings.flight_daily_quota, 1)  # 80 %
    first = check(db, monkeypatch, t0 + timedelta(minutes=1))
    assert alerts(first, "quota_near") == [
        "new:quota_near:flights_duffel_day",
        "new:quota_near:open_meteo_day",
    ]
    text = str(ops_emails()[0]["text"])
    assert "85 of 100 used (85%" in text and "240 of 300 used (80%" in text

    # A bigger plan (higher limits): both clear.
    monkeypatch.setattr(settings, "open_meteo_calls_per_day", 1000)
    monkeypatch.setattr(settings, "flight_daily_quota", 1000)
    later = check(db, monkeypatch, t0 + timedelta(minutes=15))
    assert sorted(alerts(later, "quota_near")) == [
        "recovered:quota_near:flights_duffel_day",
        "recovered:quota_near:open_meteo_day",
    ]
    only_admins_were_emailed()


def test_a_silent_refresh_scheduler_is_alerted_then_recovered(
    db: Session, people: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = utcnow()
    ok = check(db, monkeypatch, t0, tick=t0 - timedelta(minutes=25))
    assert alerts(ok, "refresh_scheduler") == []
    silent = check(db, monkeypatch, t0 + timedelta(minutes=10), tick=t0 - timedelta(minutes=25))
    assert alerts(silent, "refresh_scheduler") == ["new:refresh_scheduler_silent"]
    assert "The last forecast refresh check was at" in str(ops_emails()[0]["text"])
    later = check(db, monkeypatch, t0 + timedelta(minutes=20))
    assert alerts(later, "refresh_scheduler") == ["recovered:refresh_scheduler_silent"]
    only_admins_were_emailed()


def test_a_slower_check_interval_is_no_silence(
    db: Session, people: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Celery's sync.tick runs every SYNC_CHECK_MINUTES (up to 180): a check 40 minutes old
    is on time at SYNC_CHECK_MINUTES=60 (the limit becomes 3 x 60 min, as for the daemon's
    heartbeat), so no alert flaps between two ticks."""
    monkeypatch.setattr(get_settings(), "sync_check_minutes", 60)
    t0 = utcnow()
    between = check(db, monkeypatch, t0, tick=t0 - timedelta(minutes=40))
    assert "refresh_scheduler_silent" not in between["problems"]  # type: ignore[operator]
    # 190 minutes after that tick: three intervals missed.
    missed = check(db, monkeypatch, t0 + timedelta(minutes=150), tick=t0 - timedelta(minutes=40))
    assert alerts(missed, "refresh_scheduler") == ["new:refresh_scheduler_silent"]
    assert "expected at least every 180 min" in str(ops_emails()[0]["text"])


def test_a_long_sync_under_a_valid_lease_is_no_silence(
    db: Session, people: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sync.tick that runs a long sync holds the bulk worker: no new tick is logged until
    it ends. While its lease is valid that is work, not silence."""
    t0 = utcnow()
    started = t0 - timedelta(minutes=50)
    db.add(
        SyncRun(
            trigger="celery",
            status="running",
            started_at=started,
            lease_expires_at=t0 + timedelta(minutes=10),
            plan={"due": True, "due_kinds": ["spots", "map"]},
            stages=[],
        )
    )
    db.commit()
    working = check(db, monkeypatch, t0, tick=started)
    assert "refresh_scheduler_silent" not in working["problems"]  # type: ignore[operator]
    # The lease ran out (a killed worker): the silence counts again.
    expired = check(db, monkeypatch, t0 + timedelta(minutes=15), tick=started)
    assert alerts(expired, "refresh_scheduler") == ["new:refresh_scheduler_silent"]


def test_failed_admin_checks_do_not_announce_a_recovery(
    db: Session, people: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the admin checks themselves fail, the problems they had raised are kept open (an
    exception is not a recovery); the failure is reported, and the next good run carries on."""
    t0 = utcnow()
    for minutes in (40, 30, 20):
        fx.sync_run(db, t0 - timedelta(minutes=minutes), status="failed", error="HTTP 500")
    raised = check(db, monkeypatch, t0)
    assert alerts(raised, "refresh_failures") == ["new:refresh_failures"]

    def broken(*_args: object, **_kwargs: object) -> list[object]:
        raise RuntimeError("synthetic failure of the admin checks")

    with monkeypatch.context() as m:
        m.setattr(health, "admin_problems", broken)
        failed = check(db, monkeypatch, t0 + timedelta(minutes=15))
    assert "new:admin_checks_failed" in failed["alerts"]  # type: ignore[operator]
    assert not [a for a in failed["alerts"] if a.startswith("recovered:")]  # type: ignore[union-attr]
    assert "refresh_failures" in failed["problems"]  # type: ignore[operator]
    back = check(db, monkeypatch, t0 + timedelta(minutes=30))
    assert back["alerts"] == ["recovered:admin_checks_failed"]
    only_admins_were_emailed()


def test_the_scheduler_check_is_off_at_zero_minutes(
    db: Session, people: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "refresh_scheduler_expected_minutes", 0)
    t0 = utcnow()
    for hours in (0, 6, 12):
        result = check(db, monkeypatch, t0 + timedelta(hours=hours), tick=t0 - timedelta(hours=5))
        assert "refresh_scheduler_silent" not in result["problems"]  # type: ignore[operator]


def test_no_refresh_check_since_monitoring_began_is_alerted(
    db: Session, people: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the daemon runner with SYNC_DAEMON_KINDS=map (the hosted profile: the spot
    refreshes come from the refresh endpoint), a daemon heartbeat is no refresh check."""
    settings = get_settings()
    monkeypatch.setattr(settings, "job_runner", "daemon")
    monkeypatch.setattr(settings, "sync_daemon_kinds", "map")
    t0 = utcnow()
    log(db, "maintenance.cleanup", t0 - timedelta(hours=2))  # monitoring began 2 h ago
    heartbeat(t0, "current")
    result = check(db, monkeypatch, t0)
    assert alerts(result, "refresh_scheduler") == ["new:refresh_scheduler_silent"]
    assert "No forecast refresh check has been recorded" in str(ops_emails()[0]["text"])


def test_users_never_get_admin_emails_and_alerts_are_deduplicated(
    db: Session, people: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = utcnow()
    for minutes in (40, 30, 20):  # manual syncs: they say nothing about the scheduler
        fx.sync_run(
            db, t0 - timedelta(minutes=minutes), trigger="cli", status="failed", error="HTTP 503"
        )
    first = check(db, monkeypatch, t0, tick=t0 - timedelta(hours=2))
    assert {"new:refresh_failures", "new:refresh_scheduler_silent"} <= set(first["alerts"])  # type: ignore[arg-type]
    for minutes in range(15, 12 * 60, 15):
        again = check(
            db, monkeypatch, t0 + timedelta(minutes=minutes), tick=t0 - timedelta(hours=2)
        )
        assert again["alerts"] == [], minutes
    assert len(ops_emails()) == 1
    reminder = check(db, monkeypatch, t0 + timedelta(hours=12), tick=t0 - timedelta(hours=2))
    assert "reminder:refresh_failures" in reminder["alerts"]  # type: ignore[operator]
    only_admins_were_emailed()
    assert len(ops_emails()) == 2
