"""The local Vercel-style stack's files (package local-vercel-stack) stay consistent with the
settings and the hosted profile, without Docker: .env.vercel.example, docker-compose.vercel.yml,
the Containerfile, the lean API image, the edge's Caddyfile and PgBouncer's configuration.

`scripts/vercel-local.sh smoke` checks the running stack; docs/local-vercel-stack.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import AliasChoices

from app.core.config import Settings
from app.core.profiles import profile_env

ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = ROOT / ".env.vercel.example"
COMPOSE = ROOT / "docker-compose.vercel.yml"
CONTAINERFILE = ROOT / "Containerfile"
API_LEAN = ROOT / "docker" / "api-lean" / "Dockerfile"
PGBOUNCER = ROOT / "docker" / "pgbouncer"
CADDYFILE = ROOT / "docker" / "edge" / "Caddyfile"
FRONTEND_DOCKERFILE = ROOT / "frontend" / "Dockerfile"

HOSTED_FREE = profile_env("hosted", "free-database")
# The stack's documented departures from the hosted profile: nothing calls a weather
# provider until .env.vercel.local chooses live sources.
LOCAL_DEFAULTS = {"FORECAST_PROVIDERS": "demo", "MAP_FIELDS_ENABLED": "false"}
# Read by docker-compose.vercel.yml or the Next.js build, not by Settings.
STACK_ONLY = {
    "VERCEL_LOCAL_DATABASE_URL",
    "VERCEL_LOCAL_DATABASE_URL_UNPOOLED",
    "VERCEL_LOCAL_REDIS_URL",
    "EDGE_PORT",
    "VERCEL_PG_PORT",
    "VERCEL_PGBOUNCER_PORT",
    "VERCEL_REDIS_PORT",
    "VERCEL_MAILPIT_PORT",
    "NEXT_PUBLIC_MAP_TILE_URL",
    "NEXT_PUBLIC_MAP_ATTRIBUTION",
    "NEXT_PUBLIC_CARTO_API_KEY",
    "MAP_TILE_HOSTS",
}
# Foundation F9: the stack's host ports.
PORTS = {
    "EDGE_PORT": "3100",
    "VERCEL_PG_PORT": "55432",
    "VERCEL_PGBOUNCER_PORT": "56432",
    "VERCEL_REDIS_PORT": "56379",
    "VERCEL_MAILPIT_PORT": "18025",
}
# Values that hold credentials: the example names them and never fills them in.
SECRET_NAMES = re.compile(r"(SECRET|TOKEN|PASSWORD|_KEY$|^DATABASE_URL|^KV_URL$|^VERCEL_LOCAL_)")


def settings_env_names() -> set[str]:
    names: set[str] = set()
    for name, field in Settings.model_fields.items():
        names.add(name.upper())
        alias = field.validation_alias
        if isinstance(alias, AliasChoices):
            names |= {c for c in alias.choices if isinstance(c, str)}
    return names


def example_env() -> dict[str, str]:
    pairs = re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", ENV_EXAMPLE.read_text(), flags=re.M)
    names = [n for n, _ in pairs]
    assert len(names) == len(set(names)), "a variable is set twice in .env.vercel.example"
    return dict(pairs)


def compose_defaults() -> dict[str, str]:
    """The default of every ``${VAR:-default}`` in docker-compose.vercel.yml (the values
    .env.vercel.local can change); one default per variable, except the connection URLs
    (VERCEL_LOCAL_*), whose default names each service's application_name."""
    text = COMPOSE.read_text()
    out: dict[str, str] = {}
    for var, default in re.findall(r"\$\{([A-Z][A-Z0-9_]+):-([^}]*)\}", text):
        if var.startswith("VERCEL_LOCAL_"):
            continue
        assert out.setdefault(var, default) == default, f"${{{var}}} has two defaults"
    # A setting is read from the variable of its own name; VERCEL_LOCAL_* redirect the
    # stack's connection URLs.
    for key, var in re.findall(r"^\s+([A-Z][A-Z0-9_]+): \$\{([A-Z][A-Z0-9_]+):-", text, re.M):
        assert key == var or var.startswith("VERCEL_LOCAL_"), f"{key} is read from ${{{var}}}"
    return out


def compose_fixed() -> dict[str, str]:
    """``KEY: value`` entries of the app environment without interpolation (fixed)."""
    block = COMPOSE.read_text().split("x-app-env: &app-env", 1)[1].split("\nx-", 1)[0]
    found = re.findall(r"^  ([A-Z][A-Z0-9_]+): (?!\$\{)(.+)$", block, flags=re.M)
    return {k: v.strip().strip('"') for k, v in found}


def test_env_example_holds_the_hosted_free_profile() -> None:
    env = example_env()
    for name, value in HOSTED_FREE.items():
        assert env.get(name) == value, f"{name}: {env.get(name)!r} != profile {value!r}"


def test_env_example_names_only_settings_or_stack_variables() -> None:
    known = settings_env_names() | STACK_ONLY
    unknown = sorted(set(example_env()) - known)
    assert not unknown, f"not a setting or a stack variable: {unknown}"


def test_env_example_has_no_secret_values_and_leaves_db_use_unpooled_to_the_image() -> None:
    env = example_env()
    filled = sorted(n for n, v in env.items() if SECRET_NAMES.search(n) and v.strip())
    assert not filled, f"credentials must stay empty in the example: {filled}"
    assert "DB_USE_UNPOOLED" not in env
    for name, port in PORTS.items():
        assert env[name] == port


def test_compose_defaults_are_the_hosted_free_profile_with_demo_sources() -> None:
    defaults = compose_defaults()
    for name, value in HOSTED_FREE.items():
        if name in {"JOB_RUNNER", "CLIENT_IP_HEADER"}:
            continue  # fixed values, below
        expected = LOCAL_DEFAULTS.get(name, value)
        assert defaults.get(name) == expected, f"{name}: {defaults.get(name)!r} != {expected!r}"
    for name, port in PORTS.items():
        assert defaults[name] == port


def test_compose_fixes_the_stack_infrastructure() -> None:
    fixed = compose_fixed()
    assert fixed["JOB_RUNNER"] == "daemon"
    assert fixed["CLIENT_IP_HEADER"] == HOSTED_FREE["CLIENT_IP_HEADER"] == "x-vercel-forwarded-for"
    assert fixed["EMAIL_PROVIDER"] == "mailpit"
    assert fixed["SMTP_HOST"] == "mailpit"
    # Production refuses Mailpit and cookies over plain HTTP.
    assert fixed["APP_ENV"] == "development"
    assert fixed["COOKIE_SECURE"] == "false"
    assert fixed["ENABLE_DEV_ENDPOINTS"] == "false"
    text = COMPOSE.read_text()
    # The API through PgBouncer, the daemon and the migration directly.
    assert "@pgbouncer:6432/swell" in text
    assert "@postgres:5432/swell?application_name=swell-sync" in text
    assert "API_ROUTING: platform" in text
    assert re.search(r"^name: swell-vercel$", text, flags=re.M)


def test_containerfile_is_the_daemon_image_without_build_args() -> None:
    text = CONTAINERFILE.read_text()
    # Vercel passes every environment variable as a build argument: none may be declared.
    assert not re.search(r"^\s*ARG\b", text, flags=re.M)
    assert "uv sync --locked --no-dev --extra sync --no-install-project" in text
    assert re.search(r"^\s+DB_USE_UNPOOLED=true \\$", text, flags=re.M)
    assert re.search(r"^\s+JOB_RUNNER=daemon$", text, flags=re.M)
    assert re.search(r"^USER swell$", text, flags=re.M)
    assert 'CMD ["python", "-m", "app.cli", "sync", "daemon"]' in text
    assert "COPY data/ /data/" in text


def test_api_image_is_lean_and_has_no_data_directory() -> None:
    text = API_LEAN.read_text()
    assert "uv sync --locked --no-dev --no-install-project" in text
    assert "--extra" not in text
    assert "data/" not in "\n".join(line for line in text.splitlines() if line.startswith("COPY"))
    assert "COPY backend/app ./app" in text
    assert "tests" not in "\n".join(line for line in text.splitlines() if line.startswith("COPY"))
    assert '"--no-proxy-headers"' in text


@pytest.mark.parametrize(
    "path", [CONTAINERFILE, API_LEAN, PGBOUNCER / "Dockerfile", FRONTEND_DOCKERFILE, COMPOSE]
)
def test_base_images_are_pinned_to_exact_versions(path: Path) -> None:
    images = re.findall(r"^(?:FROM|\s+image:)\s+(\S+)", path.read_text(), flags=re.M)
    external = [i for i in images if not i.startswith("swell-vercel/")]
    assert external
    for image in external:
        tag = image.rsplit(":", 1)[-1]
        assert re.match(r"v?\d+\.\d+(\.\d+)?(-[a-z0-9.]+)*$", tag), f"{path.name}: {image}"
        assert re.search(r"\d+\.\d+\.\d+|\d+\.\d+-", tag), f"{path.name}: {image} is not exact"


def test_edge_routes_like_vercel_services_and_overwrites_client_ip_headers() -> None:
    text = CADDYFILE.read_text()
    assert "@api path /api/*" in text
    assert "reverse_proxy api:8000" in text
    assert "reverse_proxy web:3000" in text
    for header in ("X-Vercel-Forwarded-For", "X-Real-IP", "X-Forwarded-For"):
        assert f"header_up {header} {{remote_host}}" in text
    assert "max_size 4500KB" in text
    assert "response_header_timeout 60s" in text


def test_pgbouncer_runs_in_transaction_mode_like_neon() -> None:
    ini = (PGBOUNCER / "pgbouncer.ini").read_text()
    assert re.search(r"^pool_mode = transaction$", ini, flags=re.M)
    assert re.search(r"^max_prepared_statements = 1000$", ini, flags=re.M)
    assert re.search(r"^listen_port = 6432$", ini, flags=re.M)
    assert "%include /tmp/pgbouncer/databases.ini" in ini
    assert "PGBOUNCER_PASSWORD" in (PGBOUNCER / "entrypoint.sh").read_text()


def test_frontend_dockerfile_passes_api_routing_to_the_build() -> None:
    text = FRONTEND_DOCKERFILE.read_text()
    assert re.search(r"^ARG API_ROUTING=proxy$", text, flags=re.M)
    assert "ENV API_ROUTING=${API_ROUTING}" in text
