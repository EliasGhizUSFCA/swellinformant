#!/usr/bin/env python3
"""Measure the bytes Postgres sends to the app (database egress, what Neon bills).

A counting TCP proxy sits between the app and Postgres. The app's DATABASE_URL points at the
proxy, so every byte of every connection is counted in both directions: DB -> client (Neon's
"egress": query results) and client -> DB (statements, inserted rows). Run from the
repository root with the backend's Python (the app is imported from --backend-dir):

  backend/.venv/bin/python scripts/measure_db_egress.py pipeline --redis-url URL
      A scratch database (init-db: migrate, seed, tide catalog), then one pass of
      `python -m app.cli pipeline` (run_pipeline in one session, exactly as the CLI runs it)
      with FORECAST_PROVIDERS=demo. Prints the bytes per stage, plus the archive and
      load_steps parts of the predictions, detection and matching stages.

  backend/.venv/bin/python scripts/measure_db_egress.py pipeline --template swell_live \
      --providers open_meteo,open_meteo_ecmwf --replay --no-acquire --now 2026-10-16T06:00Z \
      --redis-url URL [--replay-sources CODE] [--searches N] [--prune]
      The pass on a copy of an existing database instead (CREATE DATABASE ... TEMPLATE),
      with the FORECAST_PROVIDERS its runs came from and no provider calls:
      --replay deletes the newest blend run and the predictions of the newest run of every
      other source before the pass (setup, not counted), so the pass re-blends and re-scores
      the stored forecasts as it does after new model runs; --replay-sources CODE limits that
      to one family's newest run (and the blend), as a sync after one new model run scores.
      --searches N first adds one SYNTHETIC account (egress-measure@example.com) with N
      saved searches for every spot (setup), so the matching stage evaluates every swell
      event as it does for real users.

  backend/.venv/bin/python scripts/measure_db_egress.py pages --template swell_live \
      --providers open_meteo,open_meteo_ecmwf --redis-url URL [--publish [--no-hook]] \
      [--spot pipeline] [--now ISO]
      Page views through the API (FastAPI TestClient, anonymous visitor), each made of the
      API requests the frontend sends for it: the home page "/" (shell status, the next
      swells, the conditions now), the map page "/map" (shell, spot list, field index,
      conditions now and at the step on screen, and the default wave layer's three fields at
      that step and both neighbours, with the run key as the client sends them) and a spot
      page "/spots/<slug>" (shell, detail, forecast, timeline, events). Each page is viewed
      three times: "cold" (the public snapshot cache emptied, app.services.public_cache,
      when the measured code has one), "warm-redis" (only this process's memo emptied: a new
      serverless instance) and "warm-instance" (nothing emptied). Prints DB -> API bytes,
      Redis commands and Redis bytes per request and per page view, and for each map field
      the ratio to the payload it serves. --now freezes the app's clock (every
      app.core.database.utcnow) at that instant, so "now" is the same in every request
      and run. --publish first records a sync publication of the best servable map run and
      calls the measured code's publish hook (public_cache.note_published) when it has one
      (setup), so the published-run lookup of SYNC_PUBLISH_GATE is part of the measurement;
      --no-hook records it without the hook (the cache then derives its marker from the
      database). Any marker and payload left in that Redis database are deleted first.

Redis (--redis-url, required: a scratch Redis database, never a hosted one) is reached
through a second counting proxy, so its bytes in both directions (what Upstash counts as
bandwidth) are measured as well; commands are counted in the app's redis-py client (Upstash
bills commands; PING, which redis-py sends for idle connections, is not billed and not
counted).

  backend/.venv/bin/python scripts/measure_db_egress.py jobs --template swell_live \
      --providers open_meteo,open_meteo_ecmwf --redis-url URL --replay --searches 3 --now ISO
      The periodic jobs the sync daemon runs between syncs (its hourly batch: matching,
      flights, alerts, delivery, the staleness check and data health; and the daily
      cleanup), each once, with the app's clock frozen at --now. With --replay a pass
      (no provider calls) runs first as setup, so the jobs see the state after a sync.

The scratch database is dropped afterwards unless --keep-db. The admin connection
(--admin-url, default from PG* variables or swell:swell@localhost:5432) creates, prepares
and drops it directly, never through the proxy. Setup (init-db, alembic upgrade) goes
through the app and is reported on its own line, outside the pass total. TLS between the app
and Postgres is counted as sent (sslmode=prefer by default, as with Neon's required TLS).

Synthetic data: the demo provider's forecasts are made up (labelled demo in the app); a
--template copy measures whatever that database holds. Nothing here writes to the template.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import io
import json
import os
import re
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

REPO = Path(__file__).resolve().parent.parent
NEON_FREE_EGRESS_BYTES = 5 * 1000**3  # "5 GB of egress per project" (Neon Free plan)
SAFE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
PROTECTED = {"postgres", "template0", "template1", "swell", "swell_test", "swell_e2e"}
STAGE_FUNCTIONS = (  # app.services.pipeline names, in pipeline order
    ("acquire_all", "acquisition"),
    ("build_blend", "blending"),
    ("compute_pending_predictions", "predictions"),
    ("publish", "publish"),
    ("detect_events", "detection"),
    ("match_all", "matching"),
    ("discover_flights", "flights"),
    ("generate_alerts", "alerts"),
    ("deliver_pending", "delivery"),
)
# (module, attribute, label): parts of a stage, reported under it and not added to the total.
STAGE_PARTS = (
    ("app.services.forecasts.archive", "archive_authoritative", "predictions > archive"),
    ("app.services.swell_detection.service", "load_steps", "detection > load_steps"),
    ("app.services.matching.service", "load_steps", "matching > load_steps"),
    (
        "app.services.surf_quality.predict",
        "compute_predictions_for_run",
        "predictions > scoring (compute_predictions_for_run)",
    ),
    (
        "app.services.surf_quality.confidence_context",
        "RunConfidenceContext.load",
        "predictions > confidence context",
    ),
    ("app.services.surf_quality.predict", "tide_stages_for", "predictions > tide stages"),
    ("app.services.surf_quality.predict", "_ensemble_index", "predictions > ensemble index"),
)
MAP_PAGE_FIELDS = ("hs", "tp", "wave_dir")  # the default Wave Height layer's variables


# --------------------------------------------------------------------------- proxy
class CountingProxy:
    """TCP forwarder on 127.0.0.1 counting the bytes of every connection, both directions.

    A count is taken before the bytes are forwarded, so once the client has a reply every
    byte of it (and of the request that caused it) is counted: snapshots taken between two
    calls of the app are exact.
    """

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.db_to_client = 0
        self.client_to_db = 0
        self.connections = 0
        self.listen_port = 0
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()

    def start(self) -> int:
        threading.Thread(target=self._serve, name="egress-proxy", daemon=True).start()
        if not self._ready.wait(10):
            raise RuntimeError("the counting proxy did not start")
        return self.listen_port

    def snapshot(self) -> tuple[int, int]:
        return self.db_to_client, self.client_to_db

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        server = self._loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", 0))
        self.listen_port = server.sockets[0].getsockname()[1]
        self._ready.set()
        self._loop.run_forever()

    async def _pipe(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, to_db: bool
    ) -> None:
        try:
            while chunk := await reader.read(1 << 16):
                if to_db:
                    self.client_to_db += len(chunk)
                else:
                    self.db_to_client += len(chunk)
                writer.write(chunk)
                await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            db_reader, db_writer = await asyncio.open_connection(self.host, self.port)
        except OSError:
            writer.close()
            return
        await asyncio.gather(
            self._pipe(reader, db_writer, to_db=True), self._pipe(db_reader, writer, to_db=False)
        )


class RedisCommands:
    """Counts the commands the app's redis-py clients send (what Upstash bills): one per
    ``Redis.execute_command`` and one per queued command of a pipeline."""

    def __init__(self) -> None:
        self.count = 0

    def install(self) -> None:
        import redis.client

        original = redis.client.Redis._execute_command
        execute = redis.client.Pipeline.execute
        counter = self

        @functools.wraps(original)
        def command(client: Any, *args: Any, **options: Any) -> Any:
            counter.count += 1
            return original(client, *args, **options)

        @functools.wraps(execute)
        def pipeline(client: Any, *args: Any, **kwargs: Any) -> Any:
            counter.count += len(client.command_stack)
            return execute(client, *args, **kwargs)

        redis.client.Redis._execute_command = command  # type: ignore[method-assign,assignment]
        redis.client.Pipeline.execute = pipeline  # type: ignore[method-assign,assignment]


@dataclass
class Tally:
    db_to_client: int = 0
    client_to_db: int = 0
    seconds: float = 0.0
    calls: int = 0
    redis_commands: int = 0
    redis_bytes: int = 0  # both directions


@dataclass
class Meter:
    proxy: CountingProxy
    redis_proxy: CountingProxy | None = None
    commands: RedisCommands = field(default_factory=RedisCommands)
    tallies: dict[str, Tally] = field(default_factory=dict)

    def _redis_bytes(self) -> int:
        return sum(self.redis_proxy.snapshot()) if self.redis_proxy else 0

    @contextlib.contextmanager
    def stage(self, name: str) -> Iterator[None]:
        down, up = self.proxy.snapshot()
        commands, redis_bytes = self.commands.count, self._redis_bytes()
        started = time.perf_counter()
        try:
            yield
        finally:
            now_down, now_up = self.proxy.snapshot()
            tally = self.tallies.setdefault(name, Tally())
            tally.db_to_client += now_down - down
            tally.client_to_db += now_up - up
            tally.seconds += time.perf_counter() - started
            tally.calls += 1
            tally.redis_commands += self.commands.count - commands
            tally.redis_bytes += self._redis_bytes() - redis_bytes

    def wrap(self, module: Any, attr: str, label: str) -> bool:
        """Count calls of ``module.attr`` (``attr`` may be dotted: ``Class.method``)."""
        *path, name = attr.split(".")
        for part in path:
            module = getattr(module, part, None)
        fn = getattr(module, name, None) if module is not None else None
        if fn is None:
            return False

        @functools.wraps(fn)
        def counted(*args: Any, **kwargs: Any) -> Any:
            with self.stage(label):
                return fn(*args, **kwargs)

        setattr(module, name, counted)
        return True


# --------------------------------------------------------------------------- databases
def admin_url(arg: str | None) -> str:
    if arg:
        return arg
    user = os.environ.get("PGUSER", "swell")
    password = os.environ.get("PGPASSWORD", "swell")
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    return f"postgresql://{quote(user)}:{quote(password)}@{host}:{port}/postgres"


def admin_connect(url: str, dbname: str | None = None) -> Any:
    import psycopg

    conninfo = url if dbname is None else psycopg.conninfo.make_conninfo(url, dbname=dbname)
    return psycopg.connect(conninfo, autocommit=True)


def checked_name(name: str) -> str:
    if not SAFE_NAME.match(name) or name in PROTECTED:
        raise SystemExit(f"refusing database name {name!r}")
    return name


def create_database(url: str, name: str, template: str | None) -> None:
    with admin_connect(url) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if exists:
            raise SystemExit(f"database {name} already exists; pick another --database")
        sql = f'CREATE DATABASE "{name}"'
        if template:
            sql += f' TEMPLATE "{checked_template(template)}"'
        conn.execute(sql)


def checked_template(name: str) -> str:
    if not SAFE_NAME.match(name):
        raise SystemExit(f"refusing template name {name!r}")
    return name


def drop_database(url: str, name: str) -> None:
    with admin_connect(url) as conn:
        # Our own leftover sessions only (a non-superuser may not end anyone else's).
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s "
            "AND usename = current_user AND pid <> pg_backend_pid()",
            (name,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def app_database_url(url: str, port: int, name: str) -> str:
    parts = urlsplit(url)
    user = quote(parts.username or "", safe="")
    password = quote(parts.password or "", safe="")
    auth = f"{user}:{password}@" if password else (f"{user}@" if user else "")
    query = f"?{parts.query}" if parts.query else ""
    return f"postgresql+psycopg://{auth}127.0.0.1:{port}/{name}{query}"


def point_app_at(database_url: str, args: argparse.Namespace) -> None:
    """Environment of the measured app; set before anything imports app.core.config."""
    os.environ["DATABASE_URL"] = database_url
    os.environ["DB_USE_UNPOOLED"] = "false"  # the engine must use the proxied URL
    for name in ("DATABASE_URL_UNPOOLED", "POSTGRES_URL_NON_POOLING"):
        os.environ.pop(name, None)
    # A fresh database runs the demo provider; a copy keeps the providers its runs came
    # from (--providers), or the blend and the run selection would ignore them.
    os.environ["FORECAST_PROVIDERS"] = args.providers or "demo"
    if args.redis_url:
        os.environ["REDIS_URL"] = args.redis_url
    for name in ("KV_URL",):  # REDIS_URL wins over the alias; never a hosted Redis here
        os.environ.pop(name, None)
    # Nothing leaves the machine: console mail and SMS, demo flights.
    os.environ.update(EMAIL_PROVIDER="console", SMS_PROVIDER="console", FLIGHT_PROVIDER="demo")
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    backend = Path(args.backend_dir).resolve()
    sys.path.insert(0, str(backend))
    os.chdir(backend)  # the CLI's relative paths (alembic.ini, data dir) as from backend/


def replay_newest_runs(url: str, name: str, sources: list[str] | None = None) -> dict[str, int]:
    """Setup for a pass on a copy: drop the newest blend run and the predictions of the
    newest scored run of every other source (or only of ``sources``, e.g. the one family
    whose new run a real sync scores), so the pass re-blends and re-scores them."""
    with admin_connect(url, name) as conn:
        blend = conn.execute(
            "DELETE FROM forecast_runs WHERE id = (SELECT r.id FROM forecast_runs r JOIN "
            "forecast_sources s ON s.id = r.source_id WHERE s.code = 'blend' "
            "ORDER BY r.issued_at DESC, r.id DESC LIMIT 1)"
        ).rowcount
        newest = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT ON (r.source_id) r.id FROM forecast_runs r JOIN "
                "forecast_sources s ON s.id = r.source_id WHERE s.code <> 'blend' AND EXISTS "
                "(SELECT 1 FROM surf_quality_predictions p WHERE p.run_id = r.id) "
                "AND (%s::text[] IS NULL OR s.code = ANY(%s::text[])) "
                "ORDER BY r.source_id, r.issued_at DESC, r.id DESC",
                (sources, sources),
            )
        ]
        deleted = conn.execute(
            "DELETE FROM surf_quality_predictions WHERE run_id = ANY(%s)", (newest,)
        ).rowcount
        conn.execute(
            "UPDATE forecast_runs SET predictions_computed_at = NULL WHERE id = ANY(%s)",
            (newest,),
        )
    return {"blend_runs_deleted": blend, "member_runs_reset": len(newest), "predictions": deleted}


def add_synthetic_searches(count: int) -> None:
    """Setup: one SYNTHETIC verified account with ``count`` saved searches for every spot
    (any quality from fair, 1-30 ft), so matching evaluates every active swell event."""
    from app.auth.security import hash_password, new_token
    from app.core.database import SessionLocal, utcnow
    from app.models import NotificationPreference, User, UserProfile
    from app.schemas.searches import SearchIn
    from app.services import searches

    with SessionLocal() as db:
        user = User(
            email="egress-measure@example.com",
            full_name="Egress measurement (synthetic)",
            password_hash=hash_password(new_token(32)),  # a random password nobody knows
            password_changed_at=utcnow(),
            email_verified_at=utcnow(),
        )
        user.profile = UserProfile()
        user.notification_preferences = NotificationPreference(unsubscribe_token=new_token(24))
        db.add(user)
        db.commit()
        for i in range(count):
            data = {
                "name": f"Synthetic search {i + 1}",
                "wave_min": 1,
                "wave_max": 30,
                "min_quality": "fair",
                "min_window_hours": 2,
                "destination_mode": "all",
                "origins": ["SFO"],
                "travel": {"max_price": 5000},
            }
            searches.create_search(db, user, SearchIn.model_validate(data))


def row_counts(url: str, name: str) -> dict[str, int]:
    tables = (
        "wave_forecasts",
        "surf_quality_predictions",
        "prediction_archive",
        "swell_events",
        "opportunity_matches",
        "map_fields",
    )
    with admin_connect(url, name) as conn:
        out: dict[str, int] = {}
        for table in tables:
            row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
            out[table] = int(row[0]) if row else 0
        return out


def parse_part(spec: str) -> tuple[str, str, str]:
    """--part MODULE:ATTR:LABEL, e.g. app.services.surf_quality.predict:tide_stages_for:
    "predictions > tides" (LABEL starts with the stage it runs in and " > ")."""
    try:
        module, attr, label = spec.split(":", 2)
    except ValueError:
        raise SystemExit(f"--part {spec!r}: expected MODULE:ATTR:LABEL") from None
    if " > " not in label:
        raise SystemExit(f"--part {spec!r}: LABEL must be '<stage> > <name>'")
    return module, attr, label


# --------------------------------------------------------------------------- report
def mb(n: int) -> str:
    return f"{n / 1e6:9.2f} MB"


def kb(n: int) -> str:
    return f"{n / 1e3:9.1f} kB"


def parse_now(value: str | None) -> datetime | None:
    if value is None:
        return None
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def print_pass(result: dict[str, Any], passes_per_day: float) -> None:
    print(f"\nDB egress of one pass ({result['database']}):")
    print(f"  {'stage':<26}{'DB->client':>13}{'client->DB':>13}{'seconds':>10}")
    for name, tally in result["stages"].items():
        indent = "    " if " > " in name else "  "
        label = name.split(" > ")[-1] if " > " in name else name
        calls = f" x{tally['calls']}" if tally["calls"] > 1 else ""
        print(
            f"{indent}{label + calls:<{28 - len(indent)}}{mb(tally['db_to_client'])}"
            f"{mb(tally['client_to_db'])}{tally['seconds']:10.1f}"
        )
    total = result["total"]
    print(f"  {'pass total':<26}{mb(total['db_to_client'])}{mb(total['client_to_db'])}")
    print(
        f"  Redis during the pass: {total['redis_commands']} commands, "
        f"{total['redis_bytes'] / 1e3:.1f} kB"
    )
    if result.get("setup"):
        setup = result["setup"]
        print(
            f"  {'(setup, not in the total)':<26}{mb(setup['db_to_client'])}"
            f"{mb(setup['client_to_db'])}"
        )
    month = total["db_to_client"] * passes_per_day * 30
    print(
        f"  extrapolated: {passes_per_day:g} passes a day x 30 days = {month / 1e9:.2f} GB "
        f"DB->client ({100 * month / NEON_FREE_EGRESS_BYTES:.0f} % of Neon Free's 5 GB)"
    )
    print(f"  rows after the pass: {result['rows']}")


# --------------------------------------------------------------------------- modes
def run_pass(args: argparse.Namespace, meter: Meter) -> dict[str, Any]:
    with meter.stage("setup"):
        if args.template:
            from alembic import command
            from alembic.config import Config

            command.upgrade(Config("alembic.ini"), "head")
        else:
            from app.cli import main as cli_main

            with contextlib.redirect_stdout(io.StringIO()):
                cli_main(["init-db"])
        if args.searches:
            add_synthetic_searches(args.searches)
    setup = meter.tallies.pop("setup")

    import app.services.pipeline as pipeline
    from app.core.database import SessionLocal

    stages = [label for attr, label in STAGE_FUNCTIONS if meter.wrap(pipeline, attr, label)]
    parts = [*STAGE_PARTS, *(parse_part(spec) for spec in args.part)]
    for module_name, attr, label in parts:
        module = __import__(module_name, fromlist=["_"])
        if not meter.wrap(module, attr, label):
            print(f"note: {module_name}.{attr} not found; not counted", file=sys.stderr)

    now = parse_now(args.now)
    with meter.stage("pass"), SessionLocal() as db:
        pipeline.run_pipeline(db, now=now, acquire=not args.no_acquire)
        if args.prune:  # the sync's last stage (SYNC_PRUNE_EACH_PASS)
            from app.services.maintenance import prune_pass

            with meter.stage("prune"):
                prune_pass(db, now or datetime.now(UTC))
    if args.prune:
        stages.append("prune")
    total = meter.tallies.pop("pass")
    ordered: dict[str, Tally] = {}
    for label in stages:
        if label in meter.tallies:
            ordered[label] = meter.tallies[label]
        for _, _, part in parts:
            if part.startswith(f"{label} > ") and part in meter.tallies:
                ordered[part] = meter.tallies[part]
    top = [t for name, t in ordered.items() if " > " not in name]
    ordered["other (sessions, expire_all)"] = Tally(
        total.db_to_client - sum(t.db_to_client for t in top),
        total.client_to_db - sum(t.client_to_db for t in top),
    )
    return {
        "stages": {k: asdict(v) for k, v in ordered.items()},
        "total": asdict(total),
        "setup": asdict(setup),
    }


def freeze_clock(when: datetime) -> int:
    """Make every ``app.core.database.utcnow`` (and each module's imported copy) return
    ``when``. Call it before the app is imported and again after: modules imported later
    bind the frozen function, those imported earlier are patched. Returns how many."""
    import app.core.database as database

    original = getattr(database, "_measure_original_utcnow", database.utcnow)
    database._measure_original_utcnow = original  # type: ignore[attr-defined]

    def frozen() -> datetime:
        return when

    patched = 0
    for module in list(sys.modules.values()):
        name = getattr(module, "__name__", "")
        if name.startswith("app") and getattr(module, "utcnow", None) is original:
            module.utcnow = frozen  # type: ignore[attr-defined]
            patched += 1
    return patched


PAGE_STATES = ("cold", "warm-redis", "warm-instance")


def page_requests(kind: str, slug: str, index: dict[str, Any], now: datetime) -> list[str]:
    """The API requests the frontend sends for one view of a page (hooks/queries.ts,
    hooks/map-fields.ts, components/layout/app-shell.tsx, components/live-swells.tsx)."""
    shell = ["/api/auth/me", "/api/system/status"]  # useMe and the demo ribbon on every page
    if kind == "home":
        return [*shell, "/api/events?limit=4&min_score=57", "/api/map/conditions"]
    if kind == "spot":
        base = f"/api/spots/{slug}"
        return [
            *shell,
            base,
            f"{base}/forecast?days=10",
            f"{base}/timeline?days=10",
            f"{base}/events",
        ]
    urls = [*shell, "/api/spots", "/api/map/fields", "/api/map/conditions"]
    times: list[str] = index.get("valid_times") or []
    if times:
        i = min(range(len(times)), key=lambda k: abs(_utc(times[k]) - now))
        at = times[i]
        urls.append(f"/api/map/conditions?at={quote(at)}")
        run = quote(index["run_key"])
        for t in [at, *(times[j] for j in (i + 1, i - 1) if 0 <= j < len(times))]:
            for variable in MAP_PAGE_FIELDS:
                urls.append(f"/api/map/fields/{variable}?at={quote(t)}&run={run}")
    return urls


def _request(client: Any, meter: Meter, url: str, rows: list[dict[str, Any]], page: str) -> Any:
    label = f"{page} {url}"
    with meter.stage(label):
        resp = client.get(url)
    tally = meter.tallies.pop(label)
    rows.append(
        {
            "page": page,
            "url": url,
            "status": resp.status_code,
            "response_bytes": len(resp.content),
            "db_to_api": tally.db_to_client,
            "api_to_db": tally.client_to_db,
            "redis_commands": tally.redis_commands,
            "redis_bytes": tally.redis_bytes,
            "seconds": round(tally.seconds, 4),
            "cache_control": resp.headers.get("cache-control"),
            "cdn_cache_control": resp.headers.get("cdn-cache-control"),
        }
    )
    return resp


def public_cache_module() -> Any:
    """The measured code's public snapshot cache, or None (code before it existed)."""
    try:
        return __import__("app.services.public_cache", fromlist=["_"])
    except ImportError:
        return None


def empty_cache(state: str, meter: Meter) -> None:
    """Setup between views (not counted): "cold" empties the snapshot cache in Redis and
    this process, "warm-redis" only this process's memo (as on a new serverless instance)."""
    cache = public_cache_module()
    if cache is None or state == "warm-instance":
        return
    counted = meter.commands.count
    if state == "cold":
        cache.clear()
    else:
        cache.clear_memo()
    meter.commands.count = counted


def run_pages(args: argparse.Namespace, meter: Meter, url: str, name: str) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    with meter.stage("setup"):
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config("alembic.ini"), "head")
    setup = meter.tallies.pop("setup")
    if args.publish:
        publish_best_map_run(url, name)
    from app.main import app

    now = parse_now(args.now)
    if now is not None:
        freeze_clock(now)
    hook = None
    cache = public_cache_module()
    if cache is not None:  # setup: no marker or payload left by an earlier run in this Redis
        counted = meter.commands.count
        cache.clear()
        cache.get_redis_bytes().delete(cache.MARKER_KEY)
        if args.publish and not args.no_hook:
            hook = cache.note_published
            hook()
        meter.commands.count = counted
    rows: list[dict[str, Any]] = []
    with TestClient(app) as client:
        index = client.get("/api/map/fields").json()  # setup: the step on screen
        for kind in ("home", "map", "spot"):
            clock = now or (
                datetime.fromisoformat(index["valid_times"][0])
                if index.get("valid_times")
                else datetime.now(UTC)
            )
            for state in PAGE_STATES:
                empty_cache(state, meter)
                page = f"{kind}:{state}"
                for request_url in page_requests(kind, args.spot, index, clock):
                    _request(client, meter, request_url, rows, page)
    return {
        "requests": rows,
        "setup": asdict(setup),
        "publish_hook": hook is not None,
        "public_cache": public_cache_module() is not None,
    }


# The daemon's hourly batch (app.jobs.schedule.build_schedule, every interval floored to
# JOB_INTERVAL_FLOOR_MINUTES=60) and its daily cleanup.
HOURLY_JOBS = (
    "matching.match",
    "flights.discover",
    "alerts.generate",
    "notifications.deliver",
    "forecasts.check_staleness",
    "ops.data_health",
)
DAILY_JOBS = ("maintenance.cleanup",)


def run_jobs(args: argparse.Namespace, meter: Meter) -> dict[str, Any]:
    with meter.stage("setup"):
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config("alembic.ini"), "head")
        if args.searches:
            add_synthetic_searches(args.searches)
        if args.replay:
            import app.services.pipeline as pipeline
            from app.core.database import SessionLocal

            with SessionLocal() as db:
                pipeline.run_pipeline(db, now=parse_now(args.now), acquire=False)
    setup = meter.tallies.pop("setup")
    from app.core.database import SessionLocal
    from app.jobs.registry import job_spec

    now = parse_now(args.now)
    if now is not None:
        freeze_clock(now)
    for job in (*HOURLY_JOBS, *DAILY_JOBS):
        with meter.stage(job), SessionLocal() as db:
            job_spec(job).run(db)
    return {"jobs": {k: asdict(v) for k, v in meter.tallies.items()}, "setup": asdict(setup)}


def print_jobs(result: dict[str, Any]) -> None:
    print("\nDB egress of the periodic jobs (each run once):")
    hourly = 0
    for job, tally in result["jobs"].items():
        print(
            f"  {job:<28}{mb(tally['db_to_client'])}{mb(tally['client_to_db'])}"
            f"{tally['redis_commands']:6d} Redis commands"
        )
        if job in HOURLY_JOBS:
            hourly += tally["db_to_client"]
    print(f"  hourly batch {mb(hourly)}; x 24 x 30 = {hourly * 24 * 30 / 1e9:.2f} GB a month")


def _utc(value: str) -> datetime:
    return parse_now(value) or datetime.now(UTC)


def publish_best_map_run(url: str, name: str) -> None:
    """Setup: a sync publication of the best servable map run, as the publish stage writes."""
    with admin_connect(url, name) as conn:
        conn.execute(
            "INSERT INTO sync_runs (trigger, status, started_at, finished_at, plan, stages, "
            "published_at, map_run_id) SELECT 'cli', 'success', now(), now(), '{}', '[]', "
            "now(), (SELECT run_id FROM map_fields GROUP BY run_id, cycle_time "
            "ORDER BY cycle_time DESC, count(*) DESC, run_id DESC LIMIT 1)"
        )


def print_pages(result: dict[str, Any]) -> None:
    print("\nPer request (anonymous visitor, no CDN in front): DB->API, Redis commands and bytes")
    views: dict[str, dict[str, int]] = {}
    for row in result["requests"]:
        ratio = ""
        if "/api/map/fields/" in row["url"] and row["response_bytes"]:
            ratio = f"  {row['db_to_api'] / row['response_bytes']:.3f}x payload"
        print(
            f"  {row['page']:<19} {row['status']} {kb(row['db_to_api'])} DB "
            f"{kb(row['response_bytes'])} resp {row['redis_commands']:3d} cmd "
            f"{kb(row['redis_bytes'])} redis  {row['url'][:64]}{ratio}"
        )
        view = views.setdefault(
            row["page"],
            {"db_to_api": 0, "response_bytes": 0, "n": 0, "redis_commands": 0, "redis_bytes": 0},
        )
        for key in ("db_to_api", "response_bytes", "redis_commands", "redis_bytes"):
            view[key] += row[key]
        view["n"] += 1
    print("\nPer page view:")
    for page, view in views.items():
        per_5gb = NEON_FREE_EGRESS_BYTES / view["db_to_api"] if view["db_to_api"] else 0
        print(
            f"  {page:<19} {view['n']:3d} requests {kb(view['db_to_api'])} DB->API "
            f"{kb(view['response_bytes'])} responses {view['redis_commands']:4d} Redis "
            f"commands {kb(view['redis_bytes'])} Redis  ({per_5gb:,.0f} such views per 5 GB)"
        )
    fields = [r for r in result["requests"] if "/api/map/fields/" in r["url"]]
    if fields:
        down = sum(r["db_to_api"] for r in fields)
        payload = sum(r["response_bytes"] for r in fields)
        print(
            f"  map fields: {len(fields)} requests, {kb(down)} DB->API for {kb(payload)} "
            f"payload = {down / payload:.3f}x"
        )
    print(
        f"  public snapshot cache in the measured code: {result.get('public_cache')}; "
        f"publish hook called: {result.get('publish_hook')}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mode", choices=("pipeline", "pages", "jobs"))
    parser.add_argument("--admin-url", help="libpq URL of an admin connection (no driver)")
    parser.add_argument("--database", help="scratch database name (default swell_egress_<pid>)")
    parser.add_argument("--template", help="copy this database instead of a fresh init-db")
    parser.add_argument("--keep-db", action="store_true", help="do not drop the scratch DB")
    parser.add_argument("--backend-dir", default=str(REPO / "backend"))
    parser.add_argument(
        "--redis-url",
        required=True,
        help="REDIS_URL of the measured app (a scratch database, e.g. redis://localhost:6379/11)",
    )
    parser.add_argument("--now", help="ISO time the pass (or the map page) runs at")
    parser.add_argument("--no-acquire", action="store_true", help="pipeline without acquisition")
    parser.add_argument("--replay", action="store_true", help="re-blend and re-score (template)")
    parser.add_argument(
        "--replay-sources",
        help="--replay only the newest run of these source codes (and the blend), e.g. "
        "open_meteo_noaa for a GFS sync or open_meteo_ecmwf for an ECMWF sync",
    )
    parser.add_argument("--searches", type=int, default=0, help="synthetic saved searches")
    parser.add_argument("--providers", help="FORECAST_PROVIDERS of the measured app (default demo)")
    parser.add_argument(
        "--part",
        action="append",
        default=[],
        help="also count MODULE:ATTR:'<stage> > <name>' (repeatable; ATTR may be Class.method)",
    )
    parser.add_argument("--passes-per-day", type=float, default=6.0)
    parser.add_argument(
        "--prune",
        action="store_true",
        help="pipeline: also run the sync's prune stage (maintenance.prune_pass) in the pass",
    )
    parser.add_argument("--publish", action="store_true", help="pages: record a publication")
    parser.add_argument(
        "--no-hook",
        action="store_true",
        help="pages: --publish without calling the publish hook (the database marker is used)",
    )
    parser.add_argument("--spot", default="pipeline", help="pages: the spot page's slug")
    parser.add_argument("--json", dest="json_out", help="also write the result as JSON here")
    args = parser.parse_args(argv)
    if args.replay and not args.template:
        parser.error("--replay needs --template")
    if args.mode in ("pages", "jobs") and not args.template:
        parser.error(f"{args.mode} needs --template (a database with forecasts and map fields)")
    if args.template and not args.providers:
        parser.error("--template needs --providers (the FORECAST_PROVIDERS its runs came from)")
    if args.template and args.mode == "pipeline" and not args.no_acquire:
        parser.error("a pass on a copy runs with --no-acquire (no live provider calls)")

    if args.json_out:
        args.json_out = str(Path(args.json_out).resolve())  # before point_app_at's chdir
    url = admin_url(args.admin_url)
    name = checked_name(args.database or f"swell_egress_{os.getpid()}")
    target = urlsplit(url)
    create_database(url, name, args.template)
    try:
        proxy = CountingProxy(target.hostname or "localhost", target.port or 5432)
        port = proxy.start()
        redis_proxy = None
        if args.redis_url:
            parts = urlsplit(args.redis_url)
            redis_proxy = CountingProxy(parts.hostname or "localhost", parts.port or 6379)
            redis_port = redis_proxy.start()
            auth = parts.netloc.rpartition("@")[0]
            netloc = f"{auth}@127.0.0.1:{redis_port}" if auth else f"127.0.0.1:{redis_port}"
            args.redis_url = parts._replace(netloc=netloc).geturl()
        point_app_at(app_database_url(url, port, name), args)
        meter = Meter(proxy, redis_proxy)
        meter.commands.install()
        replay_sources = args.replay_sources.split(",") if args.replay_sources else None
        replay = replay_newest_runs(url, name, replay_sources) if args.replay else None
        if args.mode == "pipeline":
            result = run_pass(args, meter)
            result.update(database=name, template=args.template, replay=replay)
            result["rows"] = row_counts(url, name)
            print_pass(result, args.passes_per_day)
        elif args.mode == "jobs":
            result = run_jobs(args, meter)
            result.update(database=name, template=args.template, replay=replay)
            print_jobs(result)
        else:
            result = run_pages(args, meter, url, name)
            result.update(database=name, template=args.template)
            print_pages(result)
        result["connections"] = proxy.connections
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(result, indent=2, default=str))
    finally:
        from contextlib import suppress

        with suppress(Exception):
            from app.core.database import reset_engine

            reset_engine()
        if not args.keep_db:
            drop_database(url, name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
