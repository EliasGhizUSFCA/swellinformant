"""docs/API_SETUP_REQUIRED.md names only environment variables that exist.

Every `UPPER_CASE` name in backticks must be in .env.example, and must be a Settings field
unless it is read by docker-compose or the frontend only. The deprecated Open-Meteo aliases
are Settings fields that .env.example deliberately no longer sets.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import AliasChoices

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[3]
DOC = ROOT / "docs" / "API_SETUP_REQUIRED.md"
ENV_EXAMPLE = ROOT / ".env.example"

# Read by docker-compose.yml, the Docker entrypoint or the Next.js build, not by Settings.
NOT_SETTINGS = {
    "WORKER_CONCURRENCY",
    "REDIS_PORT",
    "BACKEND_URL",
    "MAP_TILE_HOSTS",
    # The Next.js build (frontend/next.config.ts): proxy or platform /api routing.
    "API_ROUTING",
    # scripts/vercel-local.sh (shell) and docker-compose.vercel.yml (its env file): the local
    # Vercel-style stack's own variables, never read by the app (docs/local-vercel-stack.md).
    "VERCEL_LOCAL_ENV_FILE",
    "VERCEL_LOCAL_COMPOSE_FILES",
    "VERCEL_LOCAL_SYNC_WAIT_SECONDS",
    "VERCEL_LOCAL_DATABASE_URL",
    "VERCEL_LOCAL_DATABASE_URL_UNPOOLED",
    "VERCEL_LOCAL_REDIS_URL",
}
DEPRECATED = {"OPEN_METEO_WAVE_MODEL", "OPEN_METEO_ATMOS_MODEL"}
# Literal values and model names that look like variables but are not.
NOT_VARIABLES = {"HTSGW", "PERPW", "DIRPW", "UGRD", "VGRD", "PRMSL", "WIND", "SWELL", "SWPER"}


def named_variables() -> set[str]:
    text = DOC.read_text()
    names = set(re.findall(r"`([A-Z][A-Z0-9_]{2,})(?:=[^`]*)?`", text))
    # Names inside the fenced env block (KEY=value lines).
    names |= set(re.findall(r"^([A-Z][A-Z0-9_]{2,})=", text, flags=re.M))
    return names - NOT_VARIABLES


def env_example_names() -> set[str]:
    return set(re.findall(r"^([A-Z][A-Z0-9_]+)=", ENV_EXAMPLE.read_text(), flags=re.M))


def setting_names() -> set[str]:
    """Every environment name Settings reads: the field names and their env aliases (KV_URL is
    an alias of REDIS_URL, the name Vercel's Upstash integration sets)."""
    names = {name.upper() for name in Settings.model_fields}
    for field in Settings.model_fields.values():
        if isinstance(field.validation_alias, AliasChoices):
            names |= {c.upper() for c in field.validation_alias.choices if isinstance(c, str)}
    return names


def test_setting_names_include_the_env_aliases() -> None:
    names = setting_names()
    assert {"REDIS_URL", "KV_URL", "DATABASE_URL_UNPOOLED", "POSTGRES_URL_NON_POOLING"} <= names
    assert "VERCEL_LOCAL_DATABASE_URL" not in names and "API_ROUTING" not in names


def test_doc_exists_and_names_the_core_services() -> None:
    text = DOC.read_text()
    for service in (
        "Open-Meteo",
        "NOAA NODD",
        "GEFS",
        "ECMWF",
        "CO-OPS",
        "EOT20",
        "NDBC",
        "Windy",
        "Duffel",
        "Amadeus",
        "Resend",
        "SendGrid",
        "Twilio",
    ):
        assert service in text, service
    assert len(named_variables()) > 60


def test_every_named_variable_exists_in_settings_and_env_example() -> None:
    fields = setting_names()
    env = env_example_names()
    for name in sorted(named_variables() - DEPRECATED):
        assert name in env, f"{name} is named in API_SETUP_REQUIRED.md but not in .env.example"
        if name.startswith("NEXT_PUBLIC_") or name in NOT_SETTINGS:
            continue
        assert name in fields, f"{name} is named in API_SETUP_REQUIRED.md but is not a setting"


def test_deprecated_aliases_are_settings_but_not_in_env_example() -> None:
    fields = {name.upper() for name in Settings.model_fields}
    env = env_example_names()
    assert named_variables() >= DEPRECATED
    for name in DEPRECATED:
        assert name in fields and name not in env, name
