"""Admin data-health alerts: every state transition of the de-duplication (no database).

Problems here are synthetic test inputs, not observations of a real system.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from celery.schedules import crontab

from app.core.config import get_settings
from app.schemas.health import (
    DataHealthOut,
    HeartbeatOut,
    MapFieldsHealthOut,
    ObservationsHealthOut,
    StorageOut,
)
from app.services.health import HealthReport, Problem, ago, overall_status
from app.services.notifications.ops_alerts import (
    ProblemState,
    mark_sent,
    plan_alerts,
    render_alert,
)
from app.workers.schedule import build_beat_schedule, describe_schedule, max_interval

T0 = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)
REPEAT = timedelta(hours=12)
STALE = Problem("forecast_stale:noaa_gfs_direct", "degraded", "NOAA run is 17 h old.")
FAILING = Problem("forecasts_unavailable", "failing", "No forecast source has a fresh run.")
JOB = Problem("job_failed:flights.discover", "degraded", "flights failed", detail="HTTP 503")


def step(
    state: dict[str, ProblemState], problems: list[Problem], now: datetime, delivered: bool = True
) -> tuple[list[str], dict[str, ProblemState]]:
    """One job run: (due items as 'reason:key', the state the run stores)."""
    plan = plan_alerts(state, problems, now, REPEAT)
    due = [f"{i.reason}:{i.key}" for i in (*plan.problems, *plan.recoveries)]
    return due, (mark_sent(plan, now) if delivered and plan.due else plan.state)


def test_ok_to_ok_sends_nothing() -> None:
    due, state = step({}, [], T0)
    assert due == [] and state == {}


def test_new_problem_alerts_once_then_is_deduplicated() -> None:
    due, state = step({}, [STALE], T0)
    assert due == [f"new:{STALE.key}"]
    assert state[STALE.key].last_alert_at == T0
    assert state[STALE.key].first_seen == T0
    for minutes in (15, 30, 60 * 11 + 59):
        due, state = step(state, [STALE], T0 + timedelta(minutes=minutes))
        assert due == [], minutes
    assert state[STALE.key].last_alert_at == T0  # unchanged while de-duplicated


def test_unresolved_problem_is_repeated_after_the_repeat_interval() -> None:
    _, state = step({}, [STALE], T0)
    due, state = step(state, [STALE], T0 + REPEAT)
    assert due == [f"reminder:{STALE.key}"]
    assert state[STALE.key].last_alert_at == T0 + REPEAT
    assert state[STALE.key].first_seen == T0  # still the original start


def test_recovery_notice_after_an_alerted_problem_clears() -> None:
    _, state = step({}, [STALE], T0)
    due, state = step(state, [], T0 + timedelta(hours=1))
    assert due == [f"recovered:{STALE.key}"]
    assert state == {}  # forgotten once the recovery was delivered
    due, _ = step(state, [], T0 + timedelta(hours=2))
    assert due == []  # exactly one recovery notice


def test_problem_that_was_never_alerted_clears_silently() -> None:
    _, state = step({}, [STALE], T0, delivered=False)
    assert state[STALE.key].last_alert_at is None
    due, state = step(state, [], T0 + timedelta(minutes=15))
    assert due == [] and state == {}


def test_failed_delivery_is_retried_on_the_next_run() -> None:
    _, state = step({}, [STALE], T0, delivered=False)
    due, state = step(state, [STALE], T0 + timedelta(minutes=15))
    assert due == [f"unsent:{STALE.key}"]
    assert state[STALE.key].first_seen == T0
    assert state[STALE.key].last_alert_at == T0 + timedelta(minutes=15)


def test_failed_recovery_notice_is_retried_and_keeps_its_resolution_time() -> None:
    _, state = step({}, [STALE], T0)
    resolved = T0 + timedelta(hours=1)
    due, state = step(state, [], resolved, delivered=False)
    assert due == [f"recovered:{STALE.key}"]
    assert state[STALE.key].resolved_at == resolved
    due, state = step(state, [], resolved + timedelta(minutes=15))
    assert due == [f"recovered:{STALE.key}"] and state == {}


def test_problem_back_before_its_recovery_notice_went_out_is_not_new() -> None:
    _, state = step({}, [STALE], T0)
    _, state = step(state, [], T0 + timedelta(hours=1), delivered=False)
    due, state = step(state, [STALE], T0 + timedelta(hours=1, minutes=15))
    assert due == []  # admins were told and never heard it cleared
    assert state[STALE.key].resolved_at is None and state[STALE.key].last_alert_at == T0


def test_degraded_to_failing_alerts_the_new_failing_problem_only() -> None:
    _, state = step({}, [STALE], T0)
    due, state = step(state, [STALE, FAILING], T0 + timedelta(minutes=15))
    assert due == [f"new:{FAILING.key}"]
    assert set(state) == {STALE.key, FAILING.key}


def test_severity_escalation_of_one_key_alerts_again() -> None:
    _, state = step({}, [STALE], T0)
    worse = Problem(STALE.key, "failing", "NOAA run is 40 h old.")
    due, state = step(state, [worse], T0 + timedelta(minutes=15))
    assert due == [f"escalated:{STALE.key}"]
    assert state[STALE.key].alerted_severity == "failing"
    due, _ = step(state, [worse], T0 + timedelta(minutes=30))
    assert due == []


def test_failing_back_to_degraded_and_ok() -> None:
    _, state = step({}, [STALE, FAILING], T0)
    due, state = step(state, [STALE], T0 + timedelta(hours=1))
    assert due == [f"recovered:{FAILING.key}"]
    due, state = step(state, [], T0 + timedelta(hours=2))
    assert due == [f"recovered:{STALE.key}"] and state == {}


def test_state_round_trips_through_the_job_log_json() -> None:
    _, state = step({}, [STALE], T0)
    restored = {k: ProblemState.from_dict(v.as_dict()) for k, v in state.items()}
    assert restored == state


def _report(problems: list[Problem]) -> HealthReport:
    out = DataHealthOut(
        status=overall_status(problems),
        checked_at=T0,
        reasons=[p.message for p in problems],
        problems=[],
        forecast_sources=[],
        jobs=[],
        map_fields=MapFieldsHealthOut(mode="off", status="off"),
        observations=ObservationsHealthOut(enabled=False),
        heartbeat=HeartbeatOut(limit_minutes=30),
        storage=StorageOut(database_bytes=0),
    )
    return HealthReport(out=out, problems=problems)


def test_alert_email_lists_due_items_details_and_already_reported_problems() -> None:
    _, state = step({}, [STALE], T0)
    plan = plan_alerts(state, [STALE, FAILING, JOB], T0 + timedelta(minutes=15), REPEAT)
    subject, text = render_alert(_report([STALE, FAILING, JOB]), plan, get_settings(), REPEAT)
    assert "FAILING: 2 problems" in subject
    assert text.index(FAILING.message) < text.index(JOB.message)  # failing first
    assert "last error: HTTP 503" in text
    assert "Other current problems (already reported):" in text and STALE.message in text
    assert "/status" in text and "/api/health/data" in text and "every 12 h" in text


def test_recovery_email_subject() -> None:
    _, state = step({}, [STALE], T0)
    plan = plan_alerts(state, [], T0 + timedelta(hours=1), REPEAT)
    subject, text = render_alert(_report([]), plan, get_settings(), REPEAT)
    assert subject.endswith("Data health recovered: all checks ok")
    assert "Resolved:" in text and STALE.message in text


def test_overall_status() -> None:
    assert overall_status([]) == "ok"
    assert overall_status([STALE]) == "degraded"
    assert overall_status([STALE, FAILING]) == "failing"


@pytest.mark.parametrize(
    ("sched", "gap", "text"),
    [
        (timedelta(minutes=2), timedelta(minutes=2), "every 2 min"),
        (timedelta(hours=3), timedelta(hours=3), "every 3 h"),
        (timedelta(hours=1), timedelta(hours=1), "every hour"),
        (crontab(hour=3, minute=30), timedelta(hours=24), "daily at 03:30 UTC"),
        (
            crontab(hour="1,7,13,19", minute=40),
            timedelta(hours=6),
            "daily at 01:40, 07:40, 13:40, 19:40 UTC",
        ),
        (crontab(hour="0,6", minute=0), timedelta(hours=18), "daily at 00:00, 06:00 UTC"),
        (crontab(hour=1, minute=0, day_of_week="mon"), timedelta(days=7), None),
    ],
)
def test_schedule_intervals(sched: object, gap: timedelta, text: str | None) -> None:
    assert max_interval(sched) == gap
    if text is not None:
        assert describe_schedule(sched) == text


def test_beat_schedule_has_the_data_health_job_at_its_configured_interval() -> None:
    settings = get_settings().model_copy(update={"data_health_check_minutes": 20})
    entry = build_beat_schedule(settings)["data-health"]
    assert entry == {"task": "ops.data_health", "schedule": timedelta(minutes=20)}


def test_ago() -> None:
    assert ago(timedelta(minutes=12)) == "12 min"
    assert ago(timedelta(hours=3, minutes=24)) == "3.4 h"
    assert ago(timedelta(days=2, hours=2, minutes=24)) == "2.1 days"
