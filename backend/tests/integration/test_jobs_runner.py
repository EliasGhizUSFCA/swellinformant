"""app.jobs.runner.run_job without Celery (foundation F5)."""

from __future__ import annotations

import time
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.redis import redis_lock
from app.jobs.runner import judge_single, run_job
from app.models import BackgroundJobLog
from app.models.enums import JobStatus

pytestmark = pytest.mark.integration


def logs(db: Session, name: str) -> list[BackgroundJobLog]:
    db.expire_all()
    return list(
        db.scalars(
            select(BackgroundJobLog)
            .where(BackgroundJobLog.job_name == name)
            .order_by(BackgroundJobLog.id)
        )
    )


def busy(db: Session) -> dict[str, Any]:
    """At least 0.1 s of CPU (time.process_time), so cpu_s is measurably above zero."""
    deadline = time.process_time() + 0.1
    total = 0
    while time.process_time() < deadline:
        total += sum(i * i for i in range(10_000))
    return {"spots": 3, "total": total}


def test_success_logs_cpu_and_hands_over_the_next_stage(db: Session) -> None:
    nexts: list[str] = []
    result = run_job("detection.detect", busy, on_success=nexts.append)
    assert result["status"] == "success" and result["spots"] == 3
    # The process CPU time the job used (resource.getrusage), not wall time and not zero.
    assert isinstance(result["cpu_s"], float) and 0.1 <= result["cpu_s"] < 30
    assert nexts == ["matching.match"]  # NEXT_STAGE["detection.detect"]
    (row,) = logs(db, "detection.detect")
    assert row.status == JobStatus.SUCCESS and row.details["cpu_s"] == result["cpu_s"]
    assert row.details["spots"] == 3 and row.finished_at is not None


def test_no_hand_over_without_chain_a_next_stage_or_a_callback(db: Session) -> None:
    nexts: list[str] = []
    run_job("detection.detect", busy, chain=False, on_success=nexts.append)
    run_job("maintenance.cleanup", busy, on_success=nexts.append)  # no next stage
    assert nexts == []
    # Without a callback (the sync orchestrator) a success simply does not chain.
    assert run_job("detection.detect", busy)["status"] == "success"


def test_a_failed_judgement_never_chains(db: Session) -> None:
    nexts: list[str] = []
    result = run_job(
        "forecasts.acquire",
        lambda _db: {"status": "failed", "message": "provider down"},
        judge=judge_single,
        on_success=nexts.append,
    )
    assert result["status"] == "failed" and nexts == []
    (row,) = logs(db, "forecasts.acquire")
    assert row.status == JobStatus.FAILED and row.error_message == "provider down"
    assert "cpu_s" in row.details


def test_a_held_lock_skips_and_an_exception_is_logged(db: Session) -> None:
    nexts: list[str] = []
    with redis_lock("job:detection.detect", ttl_seconds=60):
        skipped = run_job("detection.detect", busy, on_success=nexts.append)
    assert skipped["status"] == "skipped" and nexts == []

    def boom(_db: Session) -> None:
        raise ValueError("bad data")

    with pytest.raises(ValueError, match="bad data"):
        run_job("detection.detect", boom, on_success=nexts.append)
    rows = logs(db, "detection.detect")
    assert [r.status for r in rows] == [JobStatus.SKIPPED, JobStatus.FAILED]
    assert rows[1].error_message == "ValueError: bad data" and "cpu_s" in rows[1].details
    assert nexts == []
