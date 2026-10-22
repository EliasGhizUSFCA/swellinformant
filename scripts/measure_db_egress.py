#!/usr/bin/env python3
"""Measure the bytes Postgres sends to the app (database egress, what Neon bills).

A counting TCP proxy sits between the app and Postgres. The app's DATABASE_URL points at the
proxy, so every byte of every connection is counted in both directions: DB -> client (Neon's
"egress": query results) and client -> DB (statements, inserted rows). Run from the
repository root with the backend's Python (the app is imported from --backend-dir):

  backend/.venv/bin/python scripts/measure_db_egress.py pipeline
      A scratch database (init-db: migrate, seed, tide catalog), then one pass of
      `python -m app.cli pipeline` (run_pipeline in one session, exactly as the CLI runs it)
      with FORECAST_PROVIDERS=demo. Prints the bytes per stage, plus the archive and
      load_steps parts of the predictions, detection and matching stages.

  backend/.venv/bin/python scripts/measure_db_egress.py pipeline --template swell_live \
      --providers open_meteo,open_meteo_ecmwf --replay --no-acquire --now 2026-10-16T06:00Z
      The pass on a copy of an existing database instead (CREATE DATABASE ... TEMPLATE),
      with the FORECAST_PROVIDERS its runs came from and no provider calls:
      --replay deletes the newest blend run and the predictions of the newest run of every
      other source before the pass (setup, not counted), so the pass re-blends and re-scores
      the stored forecasts as it does after new model runs. --searches N first adds one
      SYNTHETIC account (egress-measure@example.com) with N saved searches for every spot
      (setup), so the matching stage evaluates every swell event as it does for real users.

  backend/.venv/bin/python scripts/measure_db_egress.py pages --template swell_live \
      --providers open_meteo,open_meteo_ecmwf [--publish] [--spot pipeline] [--now ISO]
      Typical page views through the API (FastAPI TestClient, anonymous visitor): the map
      page (spot list, conditions, field index and the default wave layer's three fields at
      the step on screen and both neighbours, with the run key as the client sends them) and
      a spot page (detail, forecast, timeline, events). Prints DB -> API bytes per request
      and per page view, and for each map field the ratio to the payload it serves.
      --publish first records a sync publication of the best servable map run (setup), so
      the published-run lookup of SYNC_PUBLISH_GATE is part of the measurement.

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


@dataclass
class Tally:
    db_to_client: int = 0
    client_to_db: int = 0
    seconds: float = 0.0
    calls: int = 0


@dataclass
class Meter:
    proxy: CountingProxy
    tallies: dict[str, Tally] = field(default_factory=dict)

    @contextlib.contextmanager
    def stage(self, name: str) -> Iterator[None]:
        down, up = self.proxy.snapshot()
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
    # Nothing leaves the machine: console mail and SMS, demo flights.
    os.environ.update(EMAIL_PROVIDER="console", SMS_PROVIDER="console", FLIGHT_PROVIDER="demo")
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    backend = Path(args.backend_dir).resolve()
    sys.path.insert(0, str(backend))
    os.chdir(backend)  # the CLI's relative paths (alembic.ini, data dir) as from backend/


def replay_newest_runs(url: str, name: str) -> dict[str, int]:
    """Setup for a pass on a copy: drop the newest blend run and the predictions of the
    newest scored run of every other source, so the pass re-blends and re-scores them."""
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
                "ORDER BY r.source_id, r.issued_at DESC, r.id DESC"
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
            "cache_control": resp.headers.get("cache-control"),
            "cdn_cache_control": resp.headers.get("cdn-cache-control"),
        }
    )
    return resp


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

    rows: list[dict[str, Any]] = []
    with TestClient(app) as client:
        for view in range(1, args.views + 1):
            page = f"map#{view}"
            _request(client, meter, "/api/spots", rows, page)
            index = _request(client, meter, "/api/map/fields", rows, page).json()
            _request(client, meter, "/api/map/conditions", rows, page)
            times: list[str] = index.get("valid_times") or []
            if times:
                now = parse_now(args.now) or datetime.fromisoformat(times[0])
                i = min(range(len(times)), key=lambda k: abs(_utc(times[k]) - now))
                at = times[i]
                _request(client, meter, f"/api/map/conditions?at={quote(at)}", rows, page)
                for t in [at, *(times[j] for j in (i + 1, i - 1) if 0 <= j < len(times))]:
                    for variable in MAP_PAGE_FIELDS:
                        run = quote(index["run_key"])
                        field_url = f"/api/map/fields/{variable}?at={quote(t)}&run={run}"
                        _request(client, meter, field_url, rows, page)
            page = f"spot#{view}"
            slug = args.spot
            for path in ("", "/forecast?days=10", "/timeline?days=10", "/events"):
                _request(client, meter, f"/api/spots/{slug}{path}", rows, page)
    return {"requests": rows, "setup": asdict(setup)}


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
    print("\nDB -> API bytes per request (anonymous visitor, no CDN in front):")
    views: dict[str, dict[str, int]] = {}
    for row in result["requests"]:
        ratio = ""
        if "/api/map/fields/" in row["url"] and row["response_bytes"]:
            ratio = f"  {row['db_to_api'] / row['response_bytes']:.3f}x payload"
        print(
            f"  {row['page']:<7} {row['status']} {kb(row['db_to_api'])} DB->API "
            f"{kb(row['response_bytes'])} response  {row['url'][:70]}{ratio}"
        )
        view = views.setdefault(row["page"], {"db_to_api": 0, "response_bytes": 0, "n": 0})
        view["db_to_api"] += row["db_to_api"]
        view["response_bytes"] += row["response_bytes"]
        view["n"] += 1
    print("\nPer page view:")
    for page, view in views.items():
        per_5gb = NEON_FREE_EGRESS_BYTES / view["db_to_api"] if view["db_to_api"] else 0
        print(
            f"  {page:<7} {view['n']:3d} requests {kb(view['db_to_api'])} DB->API "
            f"{kb(view['response_bytes'])} responses  ({per_5gb:,.0f} such views per 5 GB)"
        )
    fields = [r for r in result["requests"] if "/api/map/fields/" in r["url"]]
    if fields:
        down = sum(r["db_to_api"] for r in fields)
        payload = sum(r["response_bytes"] for r in fields)
        print(
            f"  map fields: {len(fields)} requests, {kb(down)} DB->API for {kb(payload)} "
            f"payload = {down / payload:.3f}x"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mode", choices=("pipeline", "pages"))
    parser.add_argument("--admin-url", help="libpq URL of an admin connection (no driver)")
    parser.add_argument("--database", help="scratch database name (default swell_egress_<pid>)")
    parser.add_argument("--template", help="copy this database instead of a fresh init-db")
    parser.add_argument("--keep-db", action="store_true", help="do not drop the scratch DB")
    parser.add_argument("--backend-dir", default=str(REPO / "backend"))
    parser.add_argument("--redis-url", help="REDIS_URL of the measured app")
    parser.add_argument("--now", help="ISO time the pass (or the map page) runs at")
    parser.add_argument("--no-acquire", action="store_true", help="pipeline without acquisition")
    parser.add_argument("--replay", action="store_true", help="re-blend and re-score (template)")
    parser.add_argument("--searches", type=int, default=0, help="synthetic saved searches")
    parser.add_argument("--providers", help="FORECAST_PROVIDERS of the measured app (default demo)")
    parser.add_argument(
        "--part",
        action="append",
        default=[],
        help="also count MODULE:ATTR:'<stage> > <name>' (repeatable; ATTR may be Class.method)",
    )
    parser.add_argument("--passes-per-day", type=float, default=6.0)
    parser.add_argument("--publish", action="store_true", help="pages: record a publication")
    parser.add_argument("--spot", default="pipeline", help="pages: the spot page's slug")
    parser.add_argument("--views", type=int, default=2, help="pages: page views of each kind")
    parser.add_argument("--json", dest="json_out", help="also write the result as JSON here")
    args = parser.parse_args(argv)
    if args.replay and not args.template:
        parser.error("--replay needs --template")
    if args.mode == "pages" and not args.template:
        parser.error("pages needs --template (a database with forecasts and map fields)")
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
        point_app_at(app_database_url(url, port, name), args)
        meter = Meter(proxy)
        replay = replay_newest_runs(url, name) if args.replay else None
        if args.mode == "pipeline":
            result = run_pass(args, meter)
            result.update(database=name, template=args.template, replay=replay)
            result["rows"] = row_counts(url, name)
            print_pass(result, args.passes_per_day)
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
