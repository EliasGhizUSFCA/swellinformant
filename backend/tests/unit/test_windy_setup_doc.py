"""docs/WINDY_API_SETUP.md covers every activation step of spec section 6 and matches the code:
the variables it names exist, the command it gives runs, the quoted testing-key warning is
the one the adapter detects, and the request figures are the adapter's."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import AliasChoices

from app.cli import main as cli_main
from app.core.config import Settings
from app.services.forecasts import provider_manager, windy

ROOT = Path(__file__).resolve().parents[3]
DOC = ROOT / "docs" / "WINDY_API_SETUP.md"
SOURCES = ROOT / "docs" / "FORECAST_SOURCES.md"
# Named in the doc but not Settings fields: the prefix it warns against using for the key.
NOT_SETTINGS = {"NEXT_PUBLIC_"}


def setting_names() -> set[str]:
    names = {name.upper() for name in Settings.model_fields}
    for field in Settings.model_fields.values():
        if isinstance(field.validation_alias, AliasChoices):
            names |= {c.upper() for c in field.validation_alias.choices if isinstance(c, str)}
    return names


def text() -> str:
    return DOC.read_text()


def test_every_variable_the_doc_names_is_a_setting() -> None:
    named = set(re.findall(r"`([A-Z][A-Z0-9_]{2,})(?:=[^`]*)?`", text()))
    named |= set(re.findall(r"\b([A-Z][A-Z0-9_]{2,})=", text()))
    # Module constants the doc points to (windy.py), not environment variables.
    constants = {"EXPECTED_LAG_HOURS", "WINDOW_HOURS"}
    assert {"WINDY_API_KEY", "WINDY_STORAGE_AUTHORIZED", "WEATHER_PROVIDER"} <= named
    missing = named - setting_names() - NOT_SETTINGS - constants
    assert not missing, f"not settings: {sorted(missing)}"
    assert constants <= set(dir(windy))


def test_the_doc_covers_every_activation_step_of_spec_section_6() -> None:
    body = text()
    steps = [
        "subscription",  # obtain subscription + permissions
        "written permission",
        "WINDY_API_KEY",  # add the key
        "WINDY_STORAGE_AUTHORIZED",  # the data-use authorization
        "WEATHER_PROVIDER",  # windy or auto
        "Redeploy",
        "provider-check",  # health check
        "/api/internal/refresh",  # test refresh
        "/api/forecasts/version",  # verify publication
        "/admin/freshness",
        "Attribution",
        "Rollback",
    ]
    lowered = body.lower()
    assert [s for s in steps if s.lower() not in lowered] == []


def test_the_quoted_testing_warning_is_the_one_the_adapter_detects() -> None:
    quoted = " ".join(
        line.removeprefix("> ").strip() for line in text().splitlines() if line.startswith("> ")
    )
    assert windy.TESTING_WARNING in quoted
    assert windy.is_testing_warning(quoted)
    assert windy.TESTING_KEY_REASON in text()


def test_the_documented_command_exists() -> None:
    assert "python -m app.cli forecast provider-check --provider windy" in text()
    try:
        cli_main(["forecast", "provider-check", "--help"])
    except SystemExit as exc:  # argparse exits 0 after printing the help
        assert exc.code == 0


def test_the_documented_figures_are_the_codes() -> None:
    body = text()
    assert f"{provider_manager.WINDY_FALLBACK_MINUTES} min" in body
    assert "3 times in a row" in body and provider_manager.UNAVAILABLE_THRESHOLD == 3
    assert "100 requests for the\n50 spots" in body or "100 requests for the 50 spots" in body
    assert windy.EXPECTED_LAG_HOURS == 4.0 and windy.WINDOW_HOURS == 4.0  # "from 4 h to 8 h"
    assert "from 4 h to 8 h" in body.replace("\n", " ")
    assert windy.OUTSIDE_WINDOW_FACTOR == 4 and "four\n  times less often" in body
    # 2 models x (4 windows x 4 h x 60/15 + 8 h outside at 60 min) sentinel checks a day.
    checks = 2 * (4 * int(windy.WINDOW_HOURS * 60 / 15) + int(24 - 4 * windy.WINDOW_HOURS))
    assert checks == 144 and "= 144" in body


def test_forecast_sources_links_the_setup_page_and_names_the_selection() -> None:
    sources = SOURCES.read_text()
    assert "WINDY_API_SETUP.md" in sources
    for phrase in (
        "WEATHER_PROVIDER=auto",
        "forecast provider-check",
        "customer-api.open-meteo.com",
    ):
        assert phrase in sources
