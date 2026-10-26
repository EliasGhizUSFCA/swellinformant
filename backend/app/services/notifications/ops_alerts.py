"""Admin data-health alerts (job ``ops.data_health``).

Every DATA_HEALTH_CHECK_MINUTES (at least JOB_INTERVAL_FLOOR_MINUTES apart; run by Celery
beat or the sync daemon, and once a day by the hosted watchdog, GET
/api/internal/cron/daily) the job builds the data-health report
(:func:`app.services.health.data_health`) and emails every active admin account with a
verified address, through the configured email provider, when:

* a problem appears (new key);
* a problem's severity rises (degraded -> failing);
* a problem is still unresolved DATA_HEALTH_ALERT_REPEAT_HOURS after the last alert about
  it (a reminder);
* a problem that was alerted has cleared (a recovery notice);
* a problem that cleared comes back, but only once DATA_HEALTH_ALERT_REPEAT_HOURS have
  passed since the last alert about it. Until then the recurrence is recorded and listed
  as "already reported" in other emails, and it clears again without a second recovery
  notice. So each problem key gets at most one problem alert per repeat interval (plus
  one recovery notice after it), even when it flaps between runs; only a rise in
  severity is sent at once.

The synced pipeline's problems (``sync_heartbeat_stale``: the daemon stopped;
``sync_failed``: the last two syncs failed; ``sync_cpu_budget``: above 80 % of
SYNC_CPU_BUDGET_HOURS; ``licence``: COMMERCIAL_USE with the free Open-Meteo API) are report
problems like any other and follow the same rules; a failed sync's error text goes into
the email (``detail``), never into the public report.

All due items of one run go into one email per recipient. The alert state lives in the
job's own log rows (``background_job_logs.details["alert_state"]`` of the newest
successful ``ops.data_health`` run), so it survives restarts without a table of its own;
job logs are kept RETENTION_JOB_LOG_DAYS (30), far longer than any repeat interval. A
cleared problem's entry is kept until the repeat interval since its last alert has passed.

An item is only marked as alerted when at least one recipient's provider accepted (or
the development inbox captured) the email; otherwise it stays due and the next run tries
again. Logs carry counts only, never addresses.

When the database itself cannot be reached (e.g. Neon's compute suspended after its
monthly quota), the report cannot be built and the admin addresses cannot be read: the
daily watchdog then emails OPS_ALERT_EMAILS instead (:func:`alert_database_unreachable`),
at most once per UTC day.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.core.redis import get_redis
from app.jobs.schedule import DATA_HEALTH_TASK
from app.models import BackgroundJobLog, User
from app.models.enums import JobStatus
from app.services.health import HealthReport, Problem, data_health, stamp
from app.services.notifications.providers import EmailMessageData, build_email_provider

logger = logging.getLogger(__name__)

SEVERITY_RANK = {"degraded": 1, "failing": 2}


@dataclass
class ProblemState:
    """What the previous runs knew about one problem key.

    ``resolved_at`` is None while the problem is present and the time it cleared
    otherwise. ``recovered_at`` is when the recovery notice for the last alerted
    occurrence was delivered: set, the admins' last word about this key is "cleared"
    (a recurrence within the repeat interval stays silent); None with ``last_alert_at``
    set, an alert is open (they were told about the problem and not yet that it cleared).
    """

    severity: str
    message: str
    first_seen: datetime  # start of the current (or last) occurrence
    last_alert_at: datetime | None = None
    alerted_severity: str | None = None
    resolved_at: datetime | None = None
    recovered_at: datetime | None = None

    @property
    def present(self) -> bool:
        return self.resolved_at is None

    @property
    def alert_open(self) -> bool:
        return self.last_alert_at is not None and self.recovered_at is None

    def as_dict(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "severity": self.severity,
            "message": self.message,
            "first_seen": self.first_seen.isoformat(),
            "last_alert_at": iso(self.last_alert_at),
            "alerted_severity": self.alerted_severity,
            "resolved_at": iso(self.resolved_at),
            "recovered_at": iso(self.recovered_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProblemState:
        def when(key: str) -> datetime | None:
            value = data.get(key)
            return datetime.fromisoformat(value) if value else None

        return cls(
            severity=str(data.get("severity", "degraded")),
            message=str(data.get("message", "")),
            first_seen=when("first_seen") or utcnow(),
            last_alert_at=when("last_alert_at"),
            alerted_severity=data.get("alerted_severity"),
            resolved_at=when("resolved_at"),
            recovered_at=when("recovered_at"),
        )


@dataclass
class DueItem:
    key: str
    reason: str  # new | escalated | reminder | unsent | recurred | recovered
    severity: str
    message: str
    first_seen: datetime
    detail: str | None = None


@dataclass
class AlertPlan:
    """What to send now, and the state to keep if the email goes out (or not)."""

    problems: list[DueItem] = field(default_factory=list)
    recoveries: list[DueItem] = field(default_factory=list)
    state: dict[str, ProblemState] = field(default_factory=dict)

    @property
    def due(self) -> bool:
        return bool(self.problems or self.recoveries)


def _escalated(severity: str, old: ProblemState) -> bool:
    return SEVERITY_RANK[severity] > SEVERITY_RANK.get(old.alerted_severity or "degraded", 1)


def plan_alerts(
    previous: dict[str, ProblemState],
    problems: list[Problem],
    now: datetime,
    repeat: timedelta,
) -> AlertPlan:
    """Decide which problems and recoveries are due (pure: no I/O).

    ``state`` describes the situation *before* anything is sent; :func:`mark_sent`
    records a delivered alert. Transitions per problem key (``repeat`` counts from the
    last delivered alert about the key):

    * absent -> present: ``new``.
    * present, never delivered: ``unsent`` (retried every run).
    * present, alert open: ``escalated`` on a severity rise, ``reminder`` after
      ``repeat``, else nothing.
    * present -> absent, alert open: ``recovered`` (retried until delivered).
    * present -> absent, never delivered (or recurrence never alerted): forgotten or kept
      silently; no recovery notice for something nobody was told about.
    * back before its recovery notice went out: the open alert simply continues.
    * back after its recovery notice: ``recurred`` once ``repeat`` has passed since the
      last alert (or ``escalated``), else recorded silently.
    * a cleared, recovered entry is dropped once ``repeat`` has passed since its alert.
    """
    plan = AlertPlan()
    current = {p.key: p for p in problems}
    for key, problem in current.items():
        old = previous.get(key)
        reason: str | None
        if old is None:
            state = ProblemState(problem.severity, problem.message, first_seen=now)
            reason = "new"
        else:
            recurrence = not old.alert_open and not old.present  # cleared and told so
            state = ProblemState(
                problem.severity,
                problem.message,
                first_seen=now if recurrence else old.first_seen,
                last_alert_at=old.last_alert_at,
                alerted_severity=old.alerted_severity,
                recovered_at=old.recovered_at,
            )
            if old.last_alert_at is None:
                reason = "unsent"
            elif _escalated(problem.severity, old):
                reason = "escalated"
            elif now - old.last_alert_at >= repeat:
                reason = "reminder" if old.alert_open else "recurred"
            else:
                reason = None
        plan.state[key] = state
        if reason is not None:
            plan.problems.append(
                DueItem(
                    key, reason, problem.severity, problem.message, state.first_seen, problem.detail
                )
            )
    for key, old in previous.items():
        if key in current:
            continue
        if old.last_alert_at is None:
            continue  # never reported, so nothing to recover from
        resolved = old.resolved_at or now
        if not old.alert_open:
            # Its recovery was already delivered (this occurrence was never alerted):
            # keep it silently while a recurrence would still be inside the interval.
            if now - old.last_alert_at < repeat:
                plan.state[key] = replace(old, resolved_at=resolved)
            continue
        plan.state[key] = replace(old, resolved_at=resolved)
        plan.recoveries.append(DueItem(key, "recovered", old.severity, old.message, old.first_seen))
    plan.problems.sort(key=lambda d: (d.severity != "failing", d.key))
    plan.recoveries.sort(key=lambda d: d.key)
    return plan


def mark_sent(plan: AlertPlan, now: datetime, repeat: timedelta) -> dict[str, ProblemState]:
    """The state after the plan's email was delivered.

    Alerted problems carry ``now`` (and an open alert); recovered problems keep their last
    alert time with ``recovered_at`` set, so a recurrence inside the repeat interval stays
    silent, or are forgotten when that interval has already passed.
    """
    state = {k: replace(v) for k, v in plan.state.items()}
    for item in plan.problems:
        s = state[item.key]
        s.last_alert_at = now
        s.alerted_severity = item.severity
        s.recovered_at = None
    for item in plan.recoveries:
        s = state[item.key]
        if s.last_alert_at is None or now - s.last_alert_at >= repeat:
            state.pop(item.key)
        else:
            s.recovered_at = now
    return state


# --------------------------------------------------------------------------- email
REASON_LABELS = {
    "new": "new",
    "escalated": "now failing",
    "reminder": "still unresolved",
    "unsent": "not yet reported",
    "recurred": "back again after it had cleared",
}


def render_alert(
    report: HealthReport, plan: AlertPlan, settings: Settings, repeat: timedelta
) -> tuple[str, str]:
    """(subject, plain-text body) of one alert email."""
    status = report.status
    if plan.problems:
        n = len(plan.problems)
        subject = f"[{settings.app_name}] Data health {status.upper()}: {n} problem" + (
            "s" if n != 1 else ""
        )
    elif status == "ok":
        subject = f"[{settings.app_name}] Data health recovered: all checks ok"
    else:
        n = len(plan.recoveries)
        subject = (
            f"[{settings.app_name}] Data health: {n} problem"
            + ("s resolved" if n != 1 else " resolved")
            + f", still {status}"
        )
    lines = [
        f"Data health: {status} (checked {stamp(report.out.checked_at)}).",
        "",
    ]
    if plan.problems:
        lines.append("Needs attention:")
        for item in plan.problems:
            lines.append(
                f"- [{item.severity}] {item.message} ({REASON_LABELS.get(item.reason, item.reason)}"
                f", since {stamp(item.first_seen)})"
            )
            if item.detail:
                lines.append(f"    last error: {item.detail[:500]}")
        lines.append("")
    if plan.recoveries:
        lines.append("Resolved:")
        for item in plan.recoveries:
            lines.append(f"- {item.message} (first seen {stamp(item.first_seen)})")
        lines.append("")
    alerted = {i.key for i in plan.problems}
    others = [p for p in report.problems if p.key not in alerted]
    if others:
        lines.append("Other current problems (already reported):")
        lines.extend(f"- [{p.severity}] {p.message}" for p in others)
        lines.append("")
    hours = repeat.total_seconds() / 3600
    lines += [
        f"Each problem is reported once, then again every {hours:g} h while it lasts; you get a "
        "notice when it clears.",
        f"Status page: {settings.app_base_url}/status",
        f"Report (JSON): {settings.api_base_url}/api/health/data",
        "Playbook: docs/operations.md, section 'Incident playbook'.",
    ]
    return subject, "\n".join(lines) + "\n"


def admin_recipients(db: Session) -> list[str]:
    """Addresses of active admin accounts with a verified email address."""
    return list(
        db.scalars(
            select(User.email)
            .where(
                User.is_admin.is_(True),
                User.is_active.is_(True),
                User.email_verified_at.is_not(None),
            )
            .order_by(User.created_at)
        ).all()
    )


def send_alert(recipients: list[str], subject: str, text: str) -> dict[str, int]:
    """One email per recipient (no shared To: line); counts only."""
    provider = build_email_provider()
    sent = failed = 0
    for to in recipients:
        try:
            provider.send(EmailMessageData(to=to, subject=subject, text=text, tag="ops_alert"))
            sent += 1
        except Exception as exc:  # one failing address must not stop the rest
            failed += 1
            logger.warning("Data-health alert not sent to one admin: %s", type(exc).__name__)
    return {"sent": sent, "failed": failed}


# --------------------------------------------------------------------------- no database
# Set (NX) for the UTC day an OPS_ALERT_EMAILS alert about the database went out.
DB_UNREACHABLE_KEY = "ops:db_unreachable_alert:{day}"
DB_UNREACHABLE_TTL_SECONDS = 26 * 3600
DB_UNREACHABLE_ALERT = "new:database_unreachable"


def alert_database_unreachable(
    error: BaseException, *, now: datetime | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """Email OPS_ALERT_EMAILS that the database cannot be reached (the watchdog's fallback:
    the admin recipients live in the database). At most once per UTC day, through a Redis
    flag; without Redis it sends at every call (the cron calls once a day). Returns counts
    and the reason when nothing was sent; logs counts only."""
    settings = settings or get_settings()
    recipients = settings.ops_alert_email_list
    if not recipients:
        return {"sent": 0, "skipped": "OPS_ALERT_EMAILS is not set"}
    if not settings.data_health_alerts_enabled:
        return {"sent": 0, "skipped": "DATA_HEALTH_ALERTS_ENABLED=false"}
    now = now or utcnow()
    key = DB_UNREACHABLE_KEY.format(day=now.astimezone(UTC).strftime("%Y%m%d"))
    flagged = False
    try:
        if not get_redis().set(key, now.isoformat(), nx=True, ex=DB_UNREACHABLE_TTL_SECONDS):
            return {"sent": 0, "skipped": "already sent today"}
        flagged = True
    except redis.RedisError as exc:
        logger.warning("Database alert sent without its daily flag: %s", type(exc).__name__)
    subject = f"[{settings.app_name}] Data health FAILING: the database cannot be reached"
    text = "\n".join(
        [
            f"The daily watchdog could not reach the database at {stamp(now)} "
            f"({type(error).__name__}). Nothing can be synced, served or alerted while it is "
            "unreachable.",
            "",
            "Check the database provider first: on Neon Free the compute is suspended once "
            "the monthly compute or egress quota is used up (Neon console: usage). Then "
            "check DATABASE_URL and DATABASE_URL_UNPOOLED in the deployment.",
            "",
            "This address gets this email because it is listed in OPS_ALERT_EMAILS; the "
            "admin accounts cannot be read while the database is down. It is sent at most "
            "once a day while the problem lasts.",
            f"Status page: {settings.app_base_url}/status",
            "Playbook: docs/operations.md, section 'Incident playbook'.",
        ]
    )
    counts = send_alert(recipients, subject, text + "\n")
    if not counts["sent"] and flagged:
        try:  # nothing went out: the next call tries again
            get_redis().delete(key)
        except redis.RedisError:
            logger.debug("database alert flag not cleared", exc_info=True)
    logger.warning(
        "Database unreachable: alert to OPS_ALERT_EMAILS, %d sent, %d failed",
        counts["sent"],
        counts["failed"],
    )
    return {**counts, "subject": subject}


# --------------------------------------------------------------------------- job
def previous_state(db: Session) -> dict[str, ProblemState]:
    row = db.scalar(
        select(BackgroundJobLog)
        .where(
            BackgroundJobLog.job_name == DATA_HEALTH_TASK,
            BackgroundJobLog.status == JobStatus.SUCCESS,
        )
        .order_by(BackgroundJobLog.started_at.desc(), BackgroundJobLog.id.desc())
        .limit(1)
    )
    if row is None:
        return {}
    raw = (row.details or {}).get("alert_state") or {}
    return {str(k): ProblemState.from_dict(v) for k, v in raw.items() if isinstance(v, dict)}


def run_data_health_check(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Build the report, send what is due, and return the job-log details (incl. state)."""
    settings = get_settings()
    now = now or utcnow()
    report = data_health(db, now=now, settings=settings)
    repeat = timedelta(hours=settings.data_health_alert_repeat_hours)
    plan = plan_alerts(previous_state(db), report.problems, now, repeat)
    email: dict[str, Any] = {"due": plan.due}
    state = plan.state
    if plan.due and not settings.data_health_alerts_enabled:
        email["skipped"] = "DATA_HEALTH_ALERTS_ENABLED=false"
    elif plan.due:
        recipients = admin_recipients(db)
        if not recipients:
            email["skipped"] = "no active admin account with a verified email address"
            logger.warning("Data health is %s but no admin can be alerted", report.status)
        else:
            subject, text = render_alert(report, plan, settings, repeat)
            counts = send_alert(recipients, subject, text)
            email.update(counts, subject=subject)
            if counts["sent"]:
                state = mark_sent(plan, now, repeat)
            logger.info(
                "Data-health alert (%s): %d sent, %d failed",
                report.status,
                counts["sent"],
                counts["failed"],
            )
    if report.status != "ok":
        logger.warning("Data health %s: %s", report.status, "; ".join(report.out.reasons))
    return {
        "status": report.status,
        "problems": [p.key for p in report.problems],
        "alerts": [f"{i.reason}:{i.key}" for i in (*plan.problems, *plan.recoveries)],
        "email": email,
        "alert_state": {k: v.as_dict() for k, v in sorted(state.items())},
    }
