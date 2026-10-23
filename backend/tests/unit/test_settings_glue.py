"""Settings guards added at the Upgrade 4 integration.

* An invalid setting never shows its value: get_settings() re-raises pydantic's error
  without the inputs (a too-short SECRET_KEY or CRON_SECRET is still a secret, and the
  error lands in the Vercel function's, the daemon's and the CLI's logs).
* Settings.engine_database_url and migration_database_url apply the same-database guard
  themselves, so no caller can bypass it.
* OPS_ALERT_EMAILS is validated and normalised, and its error names no address.

Every secret and address here is a synthetic test value.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings

BACKEND = Path(__file__).resolve().parents[2]
SECRET = "short-secret"  # synthetic: 12 characters, CRON_SECRET needs 16


def make(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


@pytest.fixture
def fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_get_settings_never_shows_a_rejected_value(
    monkeypatch: pytest.MonkeyPatch, fresh_settings: None
) -> None:
    monkeypatch.setenv("CRON_SECRET", SECRET)
    monkeypatch.setenv("SECRET_KEY", "synthetic-key")  # 13 characters, needs 16
    with pytest.raises(ValidationError) as raised:
        get_settings()
    error = raised.value
    assert SECRET not in str(error) and "synthetic-key" not in str(error)
    assert SECRET not in repr(error) and error.__cause__ is None and error.__suppress_context__
    assert {e["loc"] for e in error.errors(include_input=False)} == {
        ("cron_secret",),
        ("secret_key",),
    }
    assert "String should have at least 16 characters" in str(error)


def test_a_process_that_fails_on_its_settings_logs_no_value() -> None:
    """The whole traceback a process prints (the Vercel function, the daemon, the CLI)."""
    done = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", "from app.core.config import get_settings; get_settings()"],
        cwd=BACKEND,
        env={"PATH": "/usr/bin:/bin", "CRON_SECRET": SECRET, "HOME": str(BACKEND)},
        capture_output=True,
        text=True,
    )
    assert done.returncode == 1
    assert "cron_secret" in done.stderr and "ValidationError" in done.stderr
    assert SECRET not in done.stdout + done.stderr


def test_the_database_url_properties_apply_the_same_database_guard(
    caplog: pytest.LogCaptureFixture,
) -> None:
    scratch = "postgresql+psycopg://u:p@localhost:5432/swell_test_scratch"
    other = "postgresql+psycopg://u:p@ep-x.example/neondb"  # a stale unpooled URL
    s = make(database_url=scratch, database_url_unpooled=other, db_use_unpooled=True)
    assert s.unpooled_database_url is None
    assert s.engine_database_url == scratch and s.migration_database_url == scratch
    assert "'neondb' but DATABASE_URL names 'swell_test_scratch'" in caplog.text
    assert "u:p@" not in caplog.text
    same = make(
        database_url="postgresql+psycopg://u:p@ep-x-pooler.example/neondb",
        database_url_unpooled=other,
        db_use_unpooled=True,
    )
    assert same.engine_database_url == other == same.migration_database_url


def test_ops_alert_emails_are_validated_and_normalised() -> None:
    s = make(ops_alert_emails=" Ops@Example.COM , second@example.org,")
    assert s.ops_alert_email_list == ["Ops@example.com", "second@example.org"]
    assert (
        make().ops_alert_email_list == [] and make(ops_alert_emails=" , ").ops_alert_emails is None
    )
    with pytest.raises(ValidationError) as raised:
        make(ops_alert_emails="ok@example.com,private-person-at-example")
    text = str(raised.value)
    assert "OPS_ALERT_EMAILS entry 2 is not a valid email address" in text
    assert "private-person" not in str(raised.value.errors(include_input=False))
