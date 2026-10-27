"""``python -m app.cli forecast provider-check`` (forecasts.provider_cli): the report it
prints, its exit status and that it never prints a key. The health checks are mocked here
(tests/unit/test_provider_manager.py tests them against mocked HTTP); every key is a
SYNTHETIC test value."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.cli import main as cli_main
from app.core.config import Settings
from app.services.forecasts import provider_manager as pm
from tests.unit.test_provider_manager import NOW, WINDY_KEY, clean, windy_primary  # noqa: F401


def test_provider_check_prints_the_selection_and_never_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    s = windy_primary()
    pm.record_provider_failure(
        "windy", pm.TESTING_KEY, "synthetic testing data", now=datetime.now(UTC)
    )
    monkeypatch.setattr("app.core.config.get_settings", lambda: s)

    def fake_check(provider: str, settings: Settings, **kw: object) -> pm.HealthCheckResult:
        return pm.HealthCheckResult(provider, "ok", "mocked", NOW, 0)

    monkeypatch.setattr(pm, "health_check", fake_check)
    code = cli_main(["forecast", "provider-check"])
    out = capsys.readouterr().out
    report = json.loads(out)
    assert code == 0
    assert report["selection"]["providers"] == ["windy"]
    assert report["active"]["providers"] == ["open_meteo"]
    assert report["active"]["fallback_from"] == "windy"
    assert report["eligibility"]["windy"] == {
        "eligible": True,
        "missing": [],
        "api_key_set": True,
        "storage_authorized": True,
    }
    assert report["recorded_health"]["windy"]["last_failure_kind"] == pm.TESTING_KEY
    # Without --provider: the active primary and its fallback (here the same: Open-Meteo).
    assert [c["provider"] for c in report["health_checks"]] == ["open_meteo"]
    assert WINDY_KEY not in out
    monkeypatch.setattr(
        pm, "health_check", lambda p, st, **kw: pm.HealthCheckResult(p, "credentials", "no", NOW)
    )
    assert cli_main(["forecast", "provider-check", "--provider", "windy"]) == 1
    assert json.loads(capsys.readouterr().out)["health_checks"][0]["provider"] == "windy"


def test_provider_check_rejects_an_unknown_provider() -> None:
    with pytest.raises(SystemExit):
        cli_main(["forecast", "provider-check", "--provider", "surfline"])
