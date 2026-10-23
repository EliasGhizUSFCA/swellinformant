"""The local Vercel-style stack's files (package local-vercel-stack) stay consistent with the
settings and the hosted profile, without Docker: .env.vercel.example, docker-compose.vercel.yml,
the Containerfile, the lean API image, the edge's Caddyfile and PgBouncer's configuration.

`scripts/vercel-local.sh smoke` checks the running stack; docs/local-vercel-stack.md.
"""

from __future__ import annotations

import json
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
SCRIPT = ROOT / "scripts" / "vercel-local.sh"
VERCEL_JSON = ROOT / "vercel.json"

HOSTED_FREE = profile_env("hosted", "free-database")
# The stack's documented departures from the hosted profile: no forecast or map provider
# is called until .env.vercel.local chooses live sources, and without map layers a new spot
# run triggers a sync (the hosted free-database profile syncs for new map cycles only).
LOCAL_DEFAULTS = {
    "FORECAST_PROVIDERS": "demo",
    "MAP_FIELDS_ENABLED": "false",
    "SYNC_TRIGGER_SOURCES": "spots,map",
}
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


def compose_service(name: str) -> str:
    """The lines of one service of docker-compose.vercel.yml (indented by four spaces)."""
    match = re.search(rf"^  {name}:\n((?:    .*\n|\s*\n)+)", COMPOSE.read_text(), flags=re.M)
    assert match, f"no service {name!r}"
    return match.group(1)


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


def _load_example_settings(monkeypatch: pytest.MonkeyPatch, **filled: str) -> Settings:
    """Settings from the example's non-empty values (as Vercel would set them), plus
    ``filled``; the test process's own settings environment (tests/conftest.py) is cleared
    first, so only the example speaks."""
    for name in settings_env_names():
        monkeypatch.delenv(name, raising=False)
    env = {n: v for n, v in example_env().items() if v.strip() and n not in STACK_ONLY}
    for name, value in (env | filled).items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_env_example_is_a_valid_production_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    # The placeholders as the owner fills them in (synthetic test values) and the
    # Marketplace sets them (Neon's postgresql:// URLs, Upstash's rediss:// KV_URL).
    settings = _load_example_settings(
        monkeypatch,
        SECRET_KEY="test-only-" + "s" * 40,
        CRON_SECRET="test-only-" + "c" * 30,
        DATABASE_URL="postgresql://u:p@ep-test-pooler.example.neon.tech/neondb?sslmode=require",
        DATABASE_URL_UNPOOLED="postgresql://u:p@ep-test.example.neon.tech/neondb?sslmode=require",
        KV_URL="rediss://default:test-token@test.upstash.io:6379",
        RESEND_API_KEY="re_test_only",
    )
    assert settings.app_env == "production"
    assert settings.job_runner == "daemon"
    assert settings.client_ip_header == "x-vercel-forwarded-for"
    assert settings.engine_database_url.startswith("postgresql+psycopg://u:p@ep-test-pooler.")
    assert settings.migration_database_url.startswith("postgresql+psycopg://u:p@ep-test.example")
    assert settings.redis_url == "rediss://default:test-token@test.upstash.io:6379"
    assert settings.forecast_provider_list == ["open_meteo", "open_meteo_ecmwf"]
    assert settings.map_fields_enabled is True
    assert settings.missing_live_credentials() == []


def test_env_example_unfilled_is_refused_or_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without SECRET_KEY the production validator refuses to start.
    with pytest.raises(ValueError, match="SECRET_KEY"):
        _load_example_settings(monkeypatch)
    # With it, the empty credentials the hosted setup needs are reported, not ignored.
    settings = _load_example_settings(monkeypatch, SECRET_KEY="test-only-" + "s" * 40)
    missing = settings.missing_live_credentials()
    assert "RESEND_API_KEY" in missing
    assert "CRON_SECRET (daily watchdog)" in missing


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


def test_api_image_holds_what_the_function_bundle_holds() -> None:
    if not VERCEL_JSON.is_file():
        pytest.skip("vercel.json (package vercel-deploy) is not in this tree")
    function = json.loads(VERCEL_JSON.read_text())["services"]["api"]["functions"]["app/main.py"]
    excluded = {p.split("/")[0] for p in function["excludeFiles"].strip("{}").split(",")}
    excluded.discard("**")
    # What the final stage copies from backend/ (the build stage only installs from the lock).
    final = API_LEAN.read_text().rsplit("\nFROM ", 1)[1]
    copies = [line for line in final.splitlines() if line.startswith("COPY backend/")]
    sources = set(re.findall(r"backend/([^/\s]+)", "\n".join(copies)))
    assert sources >= {"app", "alembic", "alembic.ini", "pyproject.toml"}
    assert not sources & excluded, sorted(sources & excluded)


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
    # Vercel's responses carry no Via; Caddy's proxy adds one unless it is deleted.
    assert re.search(r"^\theader -Via$", text, flags=re.M)


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


def test_script_has_the_designed_subcommands() -> None:
    text = SCRIPT.read_text()
    commands = set(re.findall(r'^  ([a-z]+)\) cmd_\1 "\$@" ;;$', text, flags=re.M))
    designed = {"up", "down", "logs", "sync", "check", "cron", "smoke", "psql", "bundle"}
    assert designed | {"status"} <= commands, sorted(designed - commands)
    assert "scripts/check_api_bundle.py" in text


def test_smoke_checks_the_lean_api_the_pooler_and_the_client_ip() -> None:
    text = SCRIPT.read_text()
    loop = re.search(r"^  for module in ([a-z ]+); do$", text, flags=re.M)
    assert loop, "the smoke's heavy-import loop"
    assert {"pandas", "eccodes", "celery", "kombu"} <= set(loop.group(1).split())
    assert "SHOW CONFIG" in text and '"$mode" = "transaction"' in text
    for header in ("X-Vercel-Forwarded-For", "X-Forwarded-For", "X-Real-IP"):
        assert f"-H '{header}: " in text
    # The counter key is the edge's peer, digested as the API does (app.core.ratelimit).
    assert "limit_key" in text
    assert "migrate --on-build" in text


def test_the_stack_mirrors_vercel_json() -> None:
    if not VERCEL_JSON.is_file():
        pytest.skip("vercel.json (package vercel-deploy) is not in this tree")
    config = json.loads(VERCEL_JSON.read_text())
    # Vercel Cron: the cron service calls the same path at the same time of day (UTC).
    (cron,) = config["crons"]
    minute, hour, *every_day = cron["schedule"].split()
    assert every_day == ["*", "*", "*"]
    cron_service = compose_service("cron")
    assert f'CRON_AT: "{int(hour):02d}:{int(minute):02d}"' in cron_service
    assert f"CRON_PATH: {cron['path']}" in cron_service
    # The daemon: the same Containerfile, 2 GiB, the same grace period after SIGTERM.
    daemon = config["daemons"]["sync"]
    assert (daemon["root"], daemon["entrypoint"]) == ("./", "Containerfile")
    assert daemon["resources"]["requests"]["memory"] == "2Gi"
    sync_service = compose_service("sync")
    assert "<<: *sync-image" in sync_service
    assert "mem_limit: 2g" in sync_service
    assert f"stop_grace_period: {daemon['terminationGracePeriodSeconds']}s" in sync_service
    assert "dockerfile: Containerfile" in COMPOSE.read_text()
    # Services routing and the API function's maxDuration at the edge.
    destinations = {r["source"]: r["destination"]["service"] for r in config["rewrites"]}
    assert destinations == {"/api/(.*)": "api", "/(.*)": "web"}
    max_duration = config["services"]["api"]["functions"]["app/main.py"]["maxDuration"]
    assert f"response_header_timeout {max_duration}s" in CADDYFILE.read_text()
    assert config["services"]["api"]["root"] == "backend/"
    assert config["services"]["web"]["root"] == "frontend/"
