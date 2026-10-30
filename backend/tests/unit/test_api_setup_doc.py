"""docs/API_SETUP_REQUIRED.md names only environment variables that exist.

Every `UPPER_CASE` name in backticks must be in .env.example, and must be a Settings field
unless it is read by docker-compose or the frontend only. The deprecated Open-Meteo aliases
are Settings fields that .env.example deliberately no longer sets.

Section 6, the settings reference, has a row for every Settings field, and its Default and
Hosted columns are the code's defaults and the hosted profile's values.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import AliasChoices

from app.core.config import Settings
from app.core.profiles import profile_env

ROOT = Path(__file__).resolve().parents[3]
DOC = ROOT / "docs" / "API_SETUP_REQUIRED.md"
ENV_EXAMPLE = ROOT / ".env.example"
COMPOSE = ROOT / "docker-compose.yml"
ENTRYPOINT = ROOT / "backend" / "docker-entrypoint.sh"

# Read by the Next.js build (frontend/next.config.ts, frontend/lib/map-tiles.ts), not by Settings.
NOT_SETTINGS = {
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
# Read by docker-compose.yml (container settings, host ports) or the image's entrypoint only;
# the settings reference (section 6) describes them, and each must really be read there.
COMPOSE_ONLY = {
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_PORT",
    "REDIS_PORT",
    "BACKEND_PORT",
    "FRONTEND_PORT",
    "MAILPIT_UI_PORT",
    "MAILPIT_SMTP_PORT",
    "API_WORKERS",
    "WORKER_CONCURRENCY",
}


def named_variables() -> set[str]:
    text = DOC.read_text()
    names = set(re.findall(r"`([A-Z][A-Z0-9_]{2,})(?:=[^`]*)?`", text))
    # Names inside the fenced env block (KEY=value lines).
    names |= set(re.findall(r"^([A-Z][A-Z0-9_]{2,})=", text, flags=re.M))
    # An upper-case default value in the settings reference (LOG_LEVEL's `INFO`) is a value.
    values = {str(f.default) for f in Settings.model_fields.values()}
    return names - values


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
        if name in COMPOSE_ONLY:
            read_by = COMPOSE.read_text() + ENTRYPOINT.read_text()
            assert f"${{{name}" in read_by, f"{name} is not read by docker compose"
            continue
        assert name in fields, f"{name} is named in API_SETUP_REQUIRED.md but is not a setting"


def test_deprecated_aliases_are_settings_but_not_in_env_example() -> None:
    fields = {name.upper() for name in Settings.model_fields}
    env = env_example_names()
    assert named_variables() >= DEPRECATED
    for name in DEPRECATED:
        assert name in fields and name not in env, name


def reference_rows() -> dict[str, tuple[str, str]]:
    """Section 6: variable -> (Default cell, Hosted cell)."""
    text = DOC.read_text()
    section = text[text.index("## 6. Settings reference") :]
    rows: dict[str, tuple[str, str]] = {}
    for line in section.splitlines():
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        match = re.fullmatch(r"`([A-Z][A-Z0-9_]+)`", cells[0]) if cells else None
        if match:
            assert match.group(1) not in rows, f"{match.group(1)} has two rows"
            rows[match.group(1)] = (cells[1], cells[2])
    return rows


def shown(value: object) -> str | None:
    """A default as the reference prints it (None = "empty")."""
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def test_the_settings_reference_has_a_row_for_every_setting() -> None:
    rows = reference_rows()
    names = {name.upper() for name in Settings.model_fields}
    assert names - set(rows) == set(), "settings without a row in section 6"
    env = env_example_names()
    for name in rows:
        assert name in env or name in DEPRECATED, f"{name} has a row but is not in .env.example"


def test_the_settings_reference_shows_the_code_defaults_and_the_hosted_profile() -> None:
    rows = reference_rows()
    hosted = profile_env("hosted", "free-database")
    for field_name, field in Settings.model_fields.items():
        default_cell, hosted_cell = rows[field_name.upper()]
        expected = shown(field.get_default(call_default_factory=True))
        assert default_cell == ("empty" if expected is None else f"`{expected}`"), field_name
    for name, (default_cell, hosted_cell) in rows.items():
        if name in hosted:
            assert hosted_cell == f"`{hosted[name]}`", name
        else:
            assert hosted_cell == "", f"{name}: the hosted profile does not set it"
        if name not in {n.upper() for n in Settings.model_fields} and name in setting_names():
            assert default_cell.startswith("alias of `"), f"{name} is an alias"
    assert set(hosted) <= set(rows)
