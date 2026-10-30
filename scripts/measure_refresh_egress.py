#!/usr/bin/env python3
"""Measure spot-only refreshes through POST /api/internal/refresh (docs/sync.md, "Measured").

Run from the repository root with the backend's Python:

  backend/.venv/bin/python scripts/measure_refresh_egress.py --redis-url URL [--json FILE]

A scratch database (init-db: migrate, seed, tide catalog) with the demo provider (synthetic
data), then, through the route (FastAPI TestClient, in process, bearer CRON_SECRET): a first
refresh (both components), a check with nothing new, a wave-only refresh after a simulated
new wave run (``forecasts.demo.advance_demo_run``, what POST /api/dev/simulate-model-run
does), an atmosphere-only refresh after a simulated wind run, and a last check. For each call
it prints the wall time, this process's CPU, the bytes PostgreSQL sent to the app (database
egress, what Neon bills) and the app sent to PostgreSQL, the Redis commands and bytes, and
the same bytes per sync stage, counted with the counting proxies of
scripts/measure_db_egress.py (the same method). The scratch database is dropped afterwards
unless --keep-db; --redis-url must be a scratch Redis database (it is emptied).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import measure_db_egress as egress  # noqa: E402

# A scratch secret of the measured app only (never a deployment's).
SECRET = "measure-refresh-scratch-secret-0123"  # noqa: S105 (scratch app only)


def cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def measure(meter: egress.Meter) -> list[dict[str, Any]]:
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.forecasts.demo import advance_demo_run
    from app.services.sync import orchestrator

    real_stage = orchestrator._stage

    def counted_stage(name: Any, ctx: Any) -> Any:
        with meter.stage(f"stage:{name}"):
            return real_stage(name, ctx)

    orchestrator._stage = counted_stage  # type: ignore[assignment]
    results: list[dict[str, Any]] = []
    with TestClient(app) as client:

        def call(label: str) -> None:
            cpu0, wall0 = cpu_seconds(), time.perf_counter()
            with meter.stage(label):
                resp = client.post(
                    "/api/internal/refresh", headers={"Authorization": f"Bearer {SECRET}"}
                )
            wall, cpu = time.perf_counter() - wall0, cpu_seconds() - cpu0
            body = resp.json()
            tally = meter.tallies.pop(label)
            stages = {
                key.removeprefix("stage:"): meter.tallies.pop(key)
                for key in [k for k in meter.tallies if k.startswith("stage:")]
            }
            results.append(
                {
                    "label": label,
                    "http": resp.status_code,
                    "status": body.get("status"),
                    "version_ids": body.get("version_ids"),
                    "wall_s": round(wall, 3),
                    "process_cpu_s": round(cpu, 3),
                    "db_to_client_bytes": tally.db_to_client,
                    "client_to_db_bytes": tally.client_to_db,
                    "redis_commands": tally.redis_commands,
                    "redis_bytes": tally.redis_bytes,
                    "stage_bytes": {
                        name: {"db_to_client": t.db_to_client, "client_to_db": t.client_to_db}
                        for name, t in stages.items()
                        if t.db_to_client or t.client_to_db
                    },
                    "stages": body.get("stages"),
                    "refreshes": body.get("refreshes"),
                    "timings": body.get("timings"),
                }
            )

        call("first refresh (both components)")
        call("check, nothing new")
        advance_demo_run(frozenset({"wave"}))
        call("wave-only refresh")
        advance_demo_run(frozenset({"atmosphere"}))
        call("atmosphere-only refresh")
        call("check after the refreshes")
    return results


def print_results(results: list[dict[str, Any]]) -> None:
    for r in results:
        print(
            f"{r['label']}: HTTP {r['http']} {r['status']} versions {r['version_ids']}, "
            f"{r['wall_s']} s wall, {r['process_cpu_s']} CPU s, DB -> app "
            f"{egress.mb(r['db_to_client_bytes'])}, app -> DB {egress.mb(r['client_to_db_bytes'])}"
            f", Redis {r['redis_commands']} commands {egress.kb(r['redis_bytes'])}"
        )
        for name, t in r["stage_bytes"].items():
            print(
                f"    {name}: DB -> app {egress.mb(t['db_to_client'])}, "
                f"app -> DB {egress.mb(t['client_to_db'])}"
            )
        for s in r["stages"] or []:
            if s.get("status") != "skipped":
                print(f"    stage {s['name']}: {s['duration_ms']} ms, {s['cpu_s']} CPU s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--redis-url", required=True, help="a scratch Redis database URL")
    parser.add_argument("--admin-url", help="libpq URL of an admin connection (no driver)")
    parser.add_argument("--database", help="scratch database name (default swell_refresh_<pid>)")
    parser.add_argument("--keep-db", action="store_true", help="do not drop the scratch DB")
    parser.add_argument("--backend-dir", default=str(egress.REPO / "backend"))
    parser.add_argument("--json", dest="json_out", help="also write the result as JSON here")
    args = parser.parse_args(argv)
    args.providers = "demo"
    if args.json_out:
        args.json_out = str(Path(args.json_out).resolve())  # before point_app_at's chdir
    url = egress.admin_url(args.admin_url)
    name = egress.checked_name(args.database or f"swell_refresh_{os.getpid()}")
    target = urlsplit(url)
    egress.create_database(url, name, None)
    try:
        proxy = egress.CountingProxy(target.hostname or "localhost", target.port or 5432)
        port = proxy.start()
        parts = urlsplit(args.redis_url)
        redis_proxy = egress.CountingProxy(parts.hostname or "localhost", parts.port or 6379)
        redis_port = redis_proxy.start()
        auth = parts.netloc.rpartition("@")[0]
        netloc = f"{auth}@127.0.0.1:{redis_port}" if auth else f"127.0.0.1:{redis_port}"
        args.redis_url = parts._replace(netloc=netloc).geturl()
        egress.point_app_at(egress.app_database_url(url, port, name), args)
        os.environ.update(
            WEATHER_PROVIDER="demo",
            CRON_SECRET=SECRET,
            ENABLE_DEV_ENDPOINTS="true",
            APP_ENV="development",
        )
        meter = egress.Meter(proxy, redis_proxy)
        meter.commands.install()
        with meter.stage("setup"):
            from app.cli import main as cli_main

            with contextlib.redirect_stdout(io.StringIO()):
                cli_main(["init-db"])
            from app.core.redis import get_redis

            get_redis().flushdb()  # the scratch Redis database (--redis-url)
        setup = meter.tallies.pop("setup")
        results = measure(meter)
        print(
            f"setup (not counted): DB -> app {egress.mb(setup.db_to_client)}, "
            f"app -> DB {egress.mb(setup.client_to_db)}"
        )
        print_results(results)
        if args.json_out:
            out = {"database": name, "results": results, "connections": proxy.connections}
            Path(args.json_out).write_text(json.dumps(out, indent=2, default=str))
    finally:
        with contextlib.suppress(Exception):
            from app.core.database import reset_engine

            reset_engine()
        if not args.keep_db:
            egress.drop_database(url, name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
