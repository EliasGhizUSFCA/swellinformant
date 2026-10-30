#!/usr/bin/env python3
"""Load test of the public forecast reads against a running stack (spec section 22).

Simulates many anonymous visitors loading the same spot: each visitor asks the version
endpoint (GET /api/forecasts/version), then loads the spot's forecast, the spot list and the
map conditions with ``?v=<version_id>`` as the browser does (``--unversioned``: without it),
from ``--concurrency`` threads at once. Prints per route the request count, errors, p50 /
p95 / max latency, the distinct bodies and versions served and the cache headers seen, so a
run against the local Vercel-style stack (scripts/vercel-local.sh) or a deployment shows
whether one published forecast is shared by every visitor. Nothing is written.

  backend/.venv/bin/python scripts/load_test_public.py --base-url http://localhost:3000 \
      [--visitors 1000] [--concurrency 64] [--spot pipeline] [--days 10] [--unversioned] \
      [--database-url postgresql://...] [--wait-for-new-version SECONDS] [--json]

--database-url (a LOCAL database, for a local stack only): its pg_stat_database rollbacks
and commits during the run are printed, read again once PG_FLUSH_WAIT_SECONDS have passed
after the load (an idle backend may hold its counters that long). The rollbacks are the
read transactions (the public reads end each with one; autovacuum never does); the commits
also count connection start-ups and autovacuum's work, which runs at any time.
--wait-for-new-version: after the load, poll the version endpoint
(every 2 s) until another version is published (e.g. after POST /api/internal/refresh), then
load every route once more and check that the new version is served; prints how long after
``published_at`` the version endpoint and the routes served it.

Exit status 1 when a request failed or a route answered different bodies for one version
(payloads are computed per UTC hour, so a run across the top of an hour can see two).
Only the standard library and httpx (and psycopg for --database-url) are used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

VERSION_PATH = "/api/forecasts/version"


@dataclass
class RouteStats:
    seconds: list[float] = field(default_factory=list)
    errors: Counter[str] = field(default_factory=Counter)
    bodies: Counter[str] = field(default_factory=Counter)  # digest -> count
    by_version: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    versions: Counter[str] = field(default_factory=Counter)
    cdn_headers: Counter[str] = field(default_factory=Counter)
    edge_cache: Counter[str] = field(default_factory=Counter)  # x-vercel-cache when present

    def summary(self) -> dict[str, Any]:
        ms = sorted(s * 1000 for s in self.seconds)
        return {
            "requests": len(ms) + sum(self.errors.values()),
            "errors": dict(self.errors),
            "p50_ms": round(percentile(ms, 0.50), 1) if ms else None,
            "p95_ms": round(percentile(ms, 0.95), 1) if ms else None,
            "max_ms": round(ms[-1], 1) if ms else None,
            "mean_ms": round(statistics.fmean(ms), 1) if ms else None,
            "distinct_bodies": len(self.bodies),
            "versions": dict(self.versions),
            "cdn_cache_control": dict(self.cdn_headers),
            "x_vercel_cache": dict(self.edge_cache),
        }


def percentile(ordered: list[float], q: float) -> float:
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def routes(spot: str, days: int) -> dict[str, str]:
    return {
        "spot forecast": f"/api/spots/{spot}/forecast?days={days}",
        "spot list": "/api/spots",
        "map conditions": "/api/map/conditions",
    }


def with_version(url: str, version_id: Any) -> str:
    if version_id is None:
        return url
    return f"{url}{'&' if '?' in url else '?'}v={version_id}"


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:16]


# PostgreSQL (15+) lets an idle backend hold its statistics for up to 10 s before flushing.
PG_FLUSH_WAIT_SECONDS = 11.0


def pg_transactions(database_url: str) -> tuple[int, int] | None:
    """(rollbacks, commits) of the database in pg_stat_database (psycopg, if installed)."""
    try:
        import psycopg
    except ImportError:
        print("psycopg is not installed: --database-url ignored", file=sys.stderr)
        return None
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as conn:
        conn.execute("SELECT pg_stat_clear_snapshot()")
        row = conn.execute(
            "SELECT xact_rollback, xact_commit FROM pg_stat_database "
            "WHERE datname = current_database()"
        ).fetchone()
    return (int(row[0]), int(row[1])) if row else None


def run_load(
    client: httpx.Client,
    paths: dict[str, str],
    visitors: int,
    concurrency: int,
    versioned: bool,
) -> tuple[dict[str, RouteStats], float]:
    stats: dict[str, RouteStats] = defaultdict(RouteStats)
    lock = threading.Lock()

    def get(name: str, url: str) -> httpx.Response | None:
        began = time.perf_counter()
        try:
            resp = client.get(url)
        except httpx.HTTPError as exc:
            with lock:
                stats[name].errors[type(exc).__name__] += 1
            return None
        took = time.perf_counter() - began
        with lock:
            route = stats[name]
            if resp.status_code != 200:
                route.errors[f"HTTP {resp.status_code}"] += 1
                return resp
            route.seconds.append(took)
            body = digest(resp.content)
            served = resp.headers.get("x-forecast-version", "-")
            route.bodies[body] += 1
            route.versions[served] += 1
            route.by_version[served].add(body)
            route.cdn_headers[resp.headers.get("cdn-cache-control", "-")] += 1
            if "x-vercel-cache" in resp.headers:
                route.edge_cache[resp.headers["x-vercel-cache"]] += 1
        return resp

    def visitor(_: int) -> None:
        version = get("version", VERSION_PATH)
        version_id = None
        if versioned and version is not None and version.status_code == 200:
            version_id = version.json().get("version_id")
        for name, url in paths.items():
            get(name, with_version(url, version_id))

    began = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(visitor, range(visitors)))
    return stats, time.perf_counter() - began


def current_version(client: httpx.Client) -> dict[str, Any]:
    resp = client.get(VERSION_PATH)
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return data


def wait_for_new_version(
    client: httpx.Client, paths: dict[str, str], before: Any, timeout: float
) -> dict[str, Any]:
    """Poll the version endpoint until it names another version, then load each route."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        version = current_version(client)
        if version.get("version_id") != before:
            break
        time.sleep(2.0)
    else:
        return {"new_version": None, "waited_seconds": timeout}
    seen_at = datetime.now(UTC)
    published = version.get("published_at")
    out: dict[str, Any] = {
        "new_version": version.get("version_id"),
        "published_at": published,
        "version_endpoint_lag_seconds": (
            round((seen_at - datetime.fromisoformat(published)).total_seconds(), 1)
            if published
            else None
        ),
        "routes": {},
    }
    for name, url in paths.items():
        resp = client.get(with_version(url, version.get("version_id")))
        out["routes"][name] = {
            "status": resp.status_code,
            "x_forecast_version": resp.headers.get("x-forecast-version"),
            "serves_new_version": resp.headers.get("x-forecast-version")
            == str(version.get("version_id")),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", required=True, help="e.g. http://localhost:3000")
    parser.add_argument("--visitors", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--spot", default="pipeline", help="slug of the spot every visitor loads")
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--unversioned", action="store_true", help="omit ?v=")
    parser.add_argument("--timeout", type=float, default=30.0, help="per request, seconds")
    parser.add_argument("--database-url", help="a LOCAL database: pg_stat_database deltas")
    parser.add_argument("--wait-for-new-version", type=float, default=0.0, metavar="SECONDS")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    if args.visitors < 1 or args.concurrency < 1:
        parser.error("--visitors and --concurrency must be at least 1")

    paths = routes(args.spot, args.days)
    limits = httpx.Limits(
        max_connections=args.concurrency, max_keepalive_connections=args.concurrency
    )
    with httpx.Client(base_url=args.base_url, timeout=args.timeout, limits=limits) as client:
        before = current_version(client)
        pg_before = pg_transactions(args.database_url) if args.database_url else None
        stats, wall = run_load(client, paths, args.visitors, args.concurrency, not args.unversioned)
        if args.database_url:
            time.sleep(PG_FLUSH_WAIT_SECONDS)
        pg_after = pg_transactions(args.database_url) if args.database_url else None
        follow = (
            wait_for_new_version(client, paths, before.get("version_id"), args.wait_for_new_version)
            if args.wait_for_new_version > 0
            else None
        )
    result: dict[str, Any] = {
        "base_url": args.base_url,
        "visitors": args.visitors,
        "concurrency": args.concurrency,
        "versioned": not args.unversioned,
        "version_before": before.get("version_id"),
        "wall_seconds": round(wall, 2),
        "requests_per_second": round(
            sum(len(s.seconds) + sum(s.errors.values()) for s in stats.values()) / wall,
            1,
        ),
        "routes": {name: s.summary() for name, s in stats.items()},
        "pg_transactions": (
            {"rollbacks": pg_after[0] - pg_before[0], "commits": pg_after[1] - pg_before[1]}
            if pg_before is not None and pg_after is not None
            else None
        ),
        "new_version": follow,
    }
    failed = any(s.errors for s in stats.values())
    mixed = any(
        len(bodies) > 1
        for name, s in stats.items()
        if name != "version"
        for bodies in s.by_version.values()
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"{args.visitors} visitors, {args.concurrency} at once, against {args.base_url} "
            f"(version {result['version_before']}, "
            f"{'versioned' if result['versioned'] else 'unversioned'} URLs): "
            f"{result['wall_seconds']} s, {result['requests_per_second']} requests/s"
        )
        for name, s in result["routes"].items():
            print(
                f"  {name:15s} n={s['requests']:5d} errors={sum(s['errors'].values()):3d} "
                f"p50={s['p50_ms']} ms p95={s['p95_ms']} ms max={s['max_ms']} ms "
                f"bodies={s['distinct_bodies']} versions={s['versions']}"
            )
            print(f"  {'':15s} CDN-Cache-Control {s['cdn_cache_control']}")
            if s["x_vercel_cache"]:
                print(f"  {'':15s} x-vercel-cache {s['x_vercel_cache']}")
        if result["pg_transactions"] is not None:
            pg = result["pg_transactions"]
            print(
                f"database transactions during the run: rollbacks {pg['rollbacks']} (the "
                f"reads), commits {pg['commits']} (connection start-ups and autovacuum's too)"
            )
        if follow is not None:
            print(f"new version: {json.dumps(follow)}")
    return 1 if failed or mixed else 0


if __name__ == "__main__":
    sys.exit(main())
