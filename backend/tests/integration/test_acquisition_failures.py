"""Acquisition failure handling: quotas, per-provider bookkeeping, retries and chaining.

The providers here are the DEMO provider (synthetic data) under other source codes, so two
"families" can be configured without any network access.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from celery.exceptions import Retry
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import BackgroundJobLog, ForecastRun, ForecastSource
from app.models.enums import ForecastRunStatus, JobStatus
from app.services.forecasts import ingestion
from app.services.forecasts.base import FetchResult, RunInfo, SourceInfo, SpotPoint
from app.services.forecasts.demo import DemoForecastProvider
from app.services.forecasts.quota import consume_open_meteo_budget
from app.services.http import (
    PermanentProviderError,
    QuotaExhaustedError,
    RunNotServedError,
    TransientProviderError,
)
from app.workers import tasks

pytestmark = pytest.mark.integration


class FakeProvider(DemoForecastProvider):
    """Synthetic demo data under its own source code; ``fail`` replaces the fetch."""

    def __init__(self, code: str, fail: Callable[[], None] | None = None, delay: float = 0) -> None:
        super().__init__(forecast_days=3, natural_swells=False)
        self.code = code
        self.fail = fail
        self.delay = delay

    def source_info(self) -> SourceInfo:
        info = super().source_info()
        return SourceInfo(**{**info.__dict__, "code": self.code, "name": self.code})

    def fetch(self, spots: list[SpotPoint], run: RunInfo) -> FetchResult:
        if self.delay:
            import time

            time.sleep(self.delay)
        if self.fail is not None:
            self.fail()
        return super().fetch(spots, run)


def configure(monkeypatch: pytest.MonkeyPatch, providers: dict[str, FakeProvider]) -> None:
    monkeypatch.setattr(get_settings(), "forecast_providers", ",".join(providers))
    monkeypatch.setattr(ingestion, "build_provider", lambda name, *a, **k: providers[name])


def over_quota() -> None:
    # A real budget check with a one-call daily limit, as an exhausted Open-Meteo day.
    limits = Settings(_env_file=None, open_meteo_calls_per_day=1)  # type: ignore[call-arg]
    consume_open_meteo_budget(5.0, settings=limits)


def runs(db: Session) -> dict[str, ForecastRun]:
    db.expire_all()
    return {
        r.source.code: r
        for r in db.scalars(select(ForecastRun).join(ForecastSource).order_by(ForecastRun.id))
    }


def test_quota_exhaustion_fails_one_provider_and_the_next_still_runs(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(monkeypatch, {"one": FakeProvider("one", over_quota), "two": FakeProvider("two")})
    outcomes = ingestion.acquire_all(db)
    assert [o.status for o in outcomes] == ["failed", "success"]
    assert "day budget" in outcomes[0].message and not outcomes[0].retryable
    by_code = runs(db)
    assert by_code["one"].status == ForecastRunStatus.FAILED  # never left RUNNING
    assert "OPEN_METEO_CALLS_PER_DAY" in (by_code["one"].error_message or "")
    assert by_code["one"].completed_at is not None
    assert by_code["two"].status == ForecastRunStatus.SUCCESS
    assert issubclass(QuotaExhaustedError, PermanentProviderError)


def test_providers_get_their_own_start_times(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(monkeypatch, {"one": FakeProvider("one", delay=0.3), "two": FakeProvider("two")})
    ingestion.acquire_all(db)
    by_code = runs(db)
    one, two = by_code["one"], by_code["two"]
    assert one.completed_at is not None and two.completed_at is not None
    assert two.started_at >= one.completed_at > one.started_at
    assert (one.completed_at - one.started_at).total_seconds() >= 0.3


def test_runs_record_stats_and_model_runs(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(monkeypatch, {"one": FakeProvider("one")})
    ingestion.acquire_all(db)
    details = runs(db)["one"].details
    # The demo downloads nothing; providers that do report their bytes (FetchStats).
    assert details["stats"] == {
        "bytes_downloaded": None,
        "requests": 0,
        "http_status_counts": {},
        "weighted_calls": None,
    }
    assert [m["model"] for m in details["model_runs"]] == [
        "demo-swell-generator",
        "demo-diurnal-wind",
    ]


def test_an_unexpected_error_fails_the_run_not_the_acquisition(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def bug() -> None:
        raise KeyError("hourly")

    configure(monkeypatch, {"one": FakeProvider("one", bug), "two": FakeProvider("two")})
    outcomes = ingestion.acquire_all(db)
    assert [o.status for o in outcomes] == ["failed", "success"]
    assert runs(db)["one"].status == ForecastRunStatus.FAILED
    assert "KeyError" in (runs(db)["one"].error_message or "")


def test_a_run_not_served_yet_is_skipped_without_a_run_row(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider("one")

    def ensure_servable(run: RunInfo) -> None:
        raise RunNotServedError("new run being published", retry_after_s=330.0)

    provider.ensure_servable = ensure_servable  # type: ignore[attr-defined]
    configure(monkeypatch, {"one": provider})
    (outcome,) = ingestion.acquire_all(db)
    assert outcome.status == "skipped" and outcome.retryable and outcome.retry_after_s == 330.0
    assert runs(db) == {}


def test_transient_failure_retries_the_task_and_permanent_does_not(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    retried: list[dict[str, Any]] = []

    def fake_retry(**kw: Any) -> Retry:
        retried.append(kw)
        return Retry("retry")

    monkeypatch.setattr(tasks.acquire_forecasts, "retry", fake_retry)

    def outage() -> None:
        raise TransientProviderError("HTTP 503 after 3 attempts")

    configure(monkeypatch, {"one": FakeProvider("one", outage)})
    with pytest.raises(Retry):
        tasks.acquire_forecasts()  # called directly: not eager, so the retry path runs
    assert retried == [{"countdown": 120, "max_retries": 3}]
    assert runs(db)["one"].details["retryable"] is True

    def refused() -> None:
        raise PermanentProviderError("HTTP 401: invalid API key")

    configure(monkeypatch, {"two": FakeProvider("two", refused)})
    result = tasks.acquire_forecasts()
    assert result["status"] == "failed" and len(retried) == 1


def test_all_failed_acquisition_is_logged_failed_and_does_not_chain(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused() -> None:
        raise PermanentProviderError("upstream refused every request")

    configure(
        monkeypatch, {"one": FakeProvider("one", refused), "two": FakeProvider("two", refused)}
    )
    result = tasks.acquire_forecasts.delay().get()
    assert result["status"] == "failed"
    db.expire_all()
    logs = db.scalars(select(BackgroundJobLog).order_by(BackgroundJobLog.id)).all()
    acquire = [r for r in logs if r.job_name == "forecasts.acquire"]
    assert acquire[-1].status == JobStatus.FAILED
    assert "every forecast provider failed" in (acquire[-1].error_message or "")
    assert not any(r.job_name == "forecasts.blend" for r in logs)  # nothing acquired


def test_a_stuck_run_is_judged_by_the_real_clock(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(monkeypatch, {"one": FakeProvider("one")})
    cycle_clock = datetime.now(UTC) + timedelta(hours=2)  # selects the model cycle only
    ingestion.acquire_all(db, now=cycle_clock)
    run = runs(db)["one"]
    run.status = ForecastRunStatus.RUNNING
    run.started_at = datetime.now(UTC) - timedelta(minutes=30)
    db.commit()
    # The shared ``now`` is 2.5 h after this run's start, but the run started 30 min ago:
    # it is in progress, not stuck.
    (outcome,) = ingestion.acquire_all(db, now=cycle_clock)
    assert outcome.status == "skipped" and outcome.message == "run in progress"
