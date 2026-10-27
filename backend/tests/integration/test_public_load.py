"""Spec section 22's performance test: 1,000 simulated visitors load the same spot.

Each visitor loads what a spot visit and the map need: the spot's forecast, the spot list
and the map conditions (``ROUTES``), from 64 threads at once through the ASGI app
(Starlette's TestClient), against PostgreSQL and Redis. Proven:

* one computation per route per forecast version (the builders are counted; the forecast's
  companion timeline is computed with it), whatever the concurrency;
* the upstream providers are never called: every forecast provider's ``current_run`` and
  ``fetch``, the provider factory and the real HTTP transports raise;
* the database sees a bounded number of transactions and statements (engine events and
  ``pg_stat_database``), not one per request;
* every visitor gets the same bytes for a route;
* a new version published while the cache is warm is served from the next request on, the
  previous version's payloads are no longer served (nor kept in this process's memo), and
  a tab still asking for the old version (``?v=``) gets the new data uncached.

The latency summary is printed (p50 / p95 / max per route, on this machine, in process: an
indication of the server-side cost, not of a deployment). The forecast data are SYNTHETIC
(demo provider); the versions are staged by the test.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app.api import spots as spots_api
from app.core.database import get_engine, utcnow
from app.models import ForecastRun, ForecastVersion, SurfSpot
from app.services import public_cache
from app.services.forecasts import registry, serving, versions
from app.services.forecasts.base import ATMOSPHERE, WAVE, ForecastProvider
from app.services.forecasts.versions import ComponentRun, SpotRun
from app.services.pipeline import run_pipeline

pytestmark = pytest.mark.integration

VISITORS = 1000
THREADS = 64
ROUTES = {
    "spot forecast": "/api/spots/pipeline/forecast?days=10",
    "spot list": "/api/spots",
    "map conditions": "/api/map/conditions",
}
# Transactions the whole run may open: one per computation (3 routes, the forecast's
# companion timeline inside its own), with room for the demo tide curve's own session-free
# reads; a per-request database read would open thousands.
MAX_TRANSACTIONS = 12


class ProviderCalled(AssertionError):
    pass


def no_providers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every way to reach a weather provider raises (and is recorded)."""
    calls: list[str] = []

    def forbidden(name: str) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise ProviderCalled(f"{name} called during a public read")

        return call

    def subclasses(cls: type) -> Iterator[type]:
        for sub in cls.__subclasses__():
            yield sub
            yield from subclasses(sub)

    for cls in {ForecastProvider, *subclasses(ForecastProvider)}:
        for method in ("current_run", "fetch"):
            if method in vars(cls):
                monkeypatch.setattr(cls, method, forbidden(f"{cls.__name__}.{method}"))
    monkeypatch.setattr(registry, "build_provider", forbidden("registry.build_provider"))
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", forbidden("httpx.HTTPTransport.handle_request")
    )
    monkeypatch.setattr(
        httpx.AsyncHTTPTransport,
        "handle_async_request",
        forbidden("httpx.AsyncHTTPTransport.handle_async_request"),
    )
    return calls


def builds(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """How often each route's payload is computed."""
    counts: dict[str, int] = defaultdict(int)
    lock = threading.Lock()

    def counted(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            with lock:
                counts[name] += 1
            return real(*args, **kwargs)

        return call

    monkeypatch.setattr(
        spots_api, "spot_forecast", counted("spot forecast", spots_api.spot_forecast)
    )
    monkeypatch.setattr(
        spots_api, "build_timeline", counted("spot timeline", spots_api.build_timeline)
    )
    monkeypatch.setattr(
        public_cache, "spot_summaries", counted("spot list", public_cache.spot_summaries)
    )
    monkeypatch.setattr(
        public_cache, "map_snapshot", counted("map conditions", public_cache.map_snapshot)
    )
    return counts


class DatabaseActivity:
    """Transactions and statements on the engine, and pg_stat_database's commits and
    rollbacks of this database, while active."""

    def __init__(self) -> None:
        self.transactions = 0
        self.statements = 0
        self.lock = threading.Lock()

    def begin(self, conn: Any) -> None:
        with self.lock:
            self.transactions += 1

    def statement(self, *args: Any) -> None:
        with self.lock:
            self.statements += 1

    def __enter__(self) -> DatabaseActivity:
        engine = get_engine()
        event.listen(engine, "begin", self.begin)
        event.listen(engine, "before_cursor_execute", self.statement)
        return self

    def __exit__(self, *exc: object) -> None:
        engine = get_engine()
        event.remove(engine, "begin", self.begin)
        event.remove(engine, "before_cursor_execute", self.statement)


def pg_transactions() -> int:
    """Commits plus rollbacks of this database in pg_stat_database, after every pooled
    connection flushed its counters (closed: a backend flushes its statistics on exit)."""
    engine = get_engine()
    engine.dispose()
    time.sleep(0.5)
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_stat_clear_snapshot()"))
        value = conn.execute(
            text(
                "SELECT xact_commit + xact_rollback FROM pg_stat_database "
                "WHERE datname = current_database()"
            )
        ).scalar_one()
    return int(value)


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def visit_all(
    client: TestClient, visitors: int, query: str = ""
) -> dict[str, list[tuple[int, bytes, dict[str, str], float]]]:
    """``visitors`` simulated visitors, each loading every route once, from THREADS
    threads at once. Per route: (status, body, headers, seconds) of each request."""
    results: dict[str, list[tuple[int, bytes, dict[str, str], float]]] = defaultdict(list)
    lock = threading.Lock()
    start = threading.Barrier(min(THREADS, visitors))

    def visitor(index: int) -> None:
        if index < THREADS:
            start.wait()  # the first wave arrives together: a cold-cache stampede
        for name, url in ROUTES.items():
            sep = "&" if "?" in url else "?"
            began = time.perf_counter()
            resp = client.get(f"{url}{sep}{query}" if query else url)
            took = time.perf_counter() - began
            with lock:
                results[name].append((resp.status_code, resp.content, dict(resp.headers), took))

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(visitor, range(visitors)))
    return results


def summary(results: dict[str, list[tuple[int, bytes, dict[str, str], float]]]) -> str:
    lines = []
    for name, rows in results.items():
        ms = [row[3] * 1000 for row in rows]
        lines.append(
            f"  {name:15s} n={len(rows):4d}  p50 {percentile(ms, 0.5):7.1f} ms  "
            f"p95 {percentile(ms, 0.95):7.1f} ms  max {max(ms):7.1f} ms  "
            f"mean {statistics.fmean(ms):7.1f} ms  errors {sum(r[0] != 200 for r in rows)}"
        )
    return "\n".join(lines)


def publish(db: Session, run_id: int) -> ForecastVersion:
    """A SYNTHETIC version serving ``run_id`` for every active spot, published and
    announced as the refresh does (the version marker, then the publication hook)."""
    now = utcnow()
    spot_ids = list(db.scalars(select(SurfSpot.id).where(SurfSpot.is_active)))
    version = versions.stage_version(
        db,
        provider="demo",
        source_code="demo",
        forecast_run_id=run_id,
        components=[
            ComponentRun(
                component=c,
                model=f"demo-{c}",
                init_time=now - timedelta(hours=5),
                available_at=now - timedelta(minutes=10),
                retrieved_at=now,
            )
            for c in (ATMOSPHERE, WAVE)
        ],
        spot_runs={sid: SpotRun(run_id, versions.UPDATED, now) for sid in spot_ids},
        acquired_at=now,
    )
    versions.publish_version(db, version)
    serving.refresh_version_marker(db)
    public_cache.note_published(version.id)
    return version


def test_a_thousand_visitors_share_one_computation_per_route_and_version(
    db: Session,
    few_spots: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = [public_cache.public_now() + timedelta(minutes=20)]
    monkeypatch.setattr(public_cache, "utcnow", lambda: clock[0])
    public_cache.clear()
    serving.clear_memo()
    # Two SYNTHETIC demo runs six hours apart; the first version serves the older one.
    run_pipeline(db, now=utcnow() - timedelta(hours=6), deliver=False)
    run_pipeline(db, deliver=False)
    older, newer = list(db.scalars(select(ForecastRun).order_by(ForecastRun.issued_at)))
    keys = {older.id: older.run_key, newer.id: newer.run_key}
    first = publish(db, older.id)
    db.close()

    calls = no_providers(monkeypatch)
    counts = builds(monkeypatch)
    from app.main import app

    with TestClient(app) as client:
        pg_before = pg_transactions()
        began = time.perf_counter()
        with DatabaseActivity() as activity:
            results = visit_all(client, VISITORS)
        wall = time.perf_counter() - began
        pg_after = pg_transactions()

        for name, rows in results.items():
            assert len(rows) == VISITORS, name
            assert {row[0] for row in rows} == {200}, name
            assert len({row[1] for row in rows}) == 1, name  # identical payloads
            assert {row[2]["x-forecast-version"] for row in rows} == {str(first.id)}, name
        forecast_body = results["spot forecast"][0][1]
        assert keys[older.id].encode() in forecast_body
        assert dict(counts) == {
            "spot forecast": 1,
            "spot timeline": 1,  # computed with the forecast (one read of the hours)
            "spot list": 1,
            "map conditions": 1,
        }
        assert calls == []
        assert activity.transactions <= MAX_TRANSACTIONS, activity.transactions
        assert pg_after - pg_before <= MAX_TRANSACTIONS + 5, (pg_before, pg_after)

        # Warm: another 1,000 visitors read nothing from the database.
        with DatabaseActivity() as warm:
            again = visit_all(client, VISITORS)
        assert warm.transactions == 0 and warm.statements == 0
        for name, rows in again.items():
            assert {row[1] for row in rows} == {results[name][0][1]}, name

        # A new version: the next requests serve it, computed once per route.
        with Session(get_engine()) as publisher:
            second = publish(publisher, newer.id)
        old_generation = public_cache._memo.generation
        assert old_generation is not None
        with DatabaseActivity() as fresh:
            after = visit_all(client, VISITORS // 5)
        for name, rows in after.items():
            assert {row[0] for row in rows} == {200}, name
            assert {row[2]["x-forecast-version"] for row in rows} == {str(second.id)}, name
            assert len({row[1] for row in rows}) == 1, name
            assert rows[0][1] != results[name][0][1], name  # not the old payload
        assert keys[newer.id].encode() in after["spot forecast"][0][1]
        assert keys[older.id].encode() not in after["spot forecast"][0][1]
        assert dict(counts) == {
            "spot forecast": 2,
            "spot timeline": 2,
            "spot list": 2,
            "map conditions": 2,
        }
        assert fresh.transactions <= MAX_TRANSACTIONS
        assert not any(k.startswith(old_generation) for k in public_cache._memo.entries)
        # An old tab still asking for the first version: the new data, never cached.
        stale_tab = visit_all(client, 50, query=f"v={first.id}")
        for name, rows in stale_tab.items():
            assert {row[1] for row in rows} == {after[name][0][1]}, name
            assert {row[2]["cdn-cache-control"] for row in rows} == {"no-store"}, name
            assert {row[2]["cache-control"] for row in rows} == {"no-store"}, name
        current_tab = visit_all(client, 50, query=f"v={second.id}")
        for name, rows in current_tab.items():
            assert {row[2]["cdn-cache-control"] for row in rows} == {
                "public, s-maxage=300, stale-while-revalidate=3600"
            }, name
        assert calls == []
        assert dict(counts)["spot list"] == 2  # the ?v= requests computed nothing

    with capsys.disabled():
        print(
            f"\n1,000 visitors x {len(ROUTES)} routes, {THREADS} threads, cold cache: "
            f"wall {wall:.2f} s, engine transactions {activity.transactions}, statements "
            f"{activity.statements}, pg_stat_database transactions "
            f"{pg_after - pg_before} (includes this test's own reads of it)\n"
            f"{summary(results)}\n"
            f"warm cache (1,000 more visitors): engine transactions {warm.transactions}, "
            f"statements {warm.statements}\n{summary(again)}\n"
            f"after a new version ({VISITORS // 5} visitors): engine transactions "
            f"{fresh.transactions}, statements {fresh.statements}\n{summary(after)}"
        )
