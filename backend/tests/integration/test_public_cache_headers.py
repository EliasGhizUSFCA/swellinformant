"""Cache headers of the user-independent public GETs. Owned by the api-serverless package.

The user-independent public GETs below send app.core.http_cache.cache_headers(browser 60 s,
CDN 300 s, stale-while-revalidate 3600 s): ``Cache-Control: public, max-age=60`` and
``CDN-Cache-Control: public, s-maxage=300, stale-while-revalidate=3600`` (Vercel's CDN reads
CDN-Cache-Control before Cache-Control and caches a function response only with s-maxage).
They were ``public, max-age=300`` (browser only) and, for /api/spots, the middleware's
``no-store`` before Upgrade 4; the CDN windows were 60 s and 300 s until the egress
integration, which serves these routes from the public snapshot cache (their data changes
only with a publication or the hour) and gives every public GET and the map-field index the
same windows, so after a publication the edge mixes old and new answers for about 5
minutes. The spot detail, forecast and events and the next swells (/api/events) read the
same published data with no user dependency and became shared-cacheable then (they were the
middleware's ``no-store``).

The constants are also what the route tests of other files assert (test_tides_api,
test_spot_observations_api, test_timeline_api, test_public_and_opportunity_api,
test_map_fields). A route with a user dependency, or a response that sets a cookie, is
never cacheable by a shared cache: :func:`test_no_route_with_a_user_dependency_is_shared_cacheable`
requests every GET route of the app signed in and checks it.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI, Response
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.auth.deps import get_auth, get_current_user, get_optional_user
from app.core.http_cache import cache_headers
from app.core.middleware import SecurityHeadersMiddleware
from app.models import User
from tests.conftest import ApiClient, search_payload

pytestmark = pytest.mark.integration

# /api/map/conditions, /api/spots/{slug}/timeline, /tides and /observations.
PUBLIC_GET_CACHE_CONTROL = "public, max-age=60"
# /api/spots: the same headers since Upgrade 4 (it was the middleware's no-store).
SPOT_LIST_CACHE_CONTROL = "public, max-age=60"
# What the CDN reads on every route above (s-maxage 60 and 300 s of stale before the egress
# integration: the snapshot data changes only with a publication or the hour).
PUBLIC_CDN_CACHE_CONTROL = "public, s-maxage=300, stale-while-revalidate=3600"

PUBLIC_GETS = (
    "/api/map/conditions",
    "/api/spots/pipeline/timeline",
    "/api/spots/pipeline/tides",
    "/api/spots/pipeline/observations",
)
# Snapshot-cached routes that were the middleware's no-store before the egress integration.
SNAPSHOT_GETS = (
    "/api/spots/pipeline",
    "/api/spots/pipeline/forecast",
    "/api/spots/pipeline/events",
    "/api/events",
)
USER_DEPENDENCIES: frozenset[Callable[..., Any]] = frozenset(
    {get_auth, get_current_user, get_optional_user}
)
SHARED_CACHE_HEADERS = ("cdn-cache-control", "vercel-cdn-cache-control")


def shared_cacheable(headers: Any) -> bool:
    control = ",".join(headers.get_list("cache-control")).lower()
    return (
        "public" in control
        or "s-maxage" in control
        or any(name in headers for name in SHARED_CACHE_HEADERS)
    )


def test_public_gets_send_cdn_cache_headers(app_client: ApiClient) -> None:
    for path in PUBLIC_GETS:
        resp = app_client.get(path)
        assert resp.status_code == 200, (path, resp.text)
        assert resp.headers.get_list("cache-control") == [PUBLIC_GET_CACHE_CONTROL], path
        assert resp.headers.get_list("cdn-cache-control") == [PUBLIC_CDN_CACHE_CONTROL], path
        assert "set-cookie" not in resp.headers, path
    spots = app_client.get("/api/spots")
    assert spots.status_code == 200
    assert spots.headers.get_list("cache-control") == [SPOT_LIST_CACHE_CONTROL]
    assert spots.headers.get_list("cdn-cache-control") == [PUBLIC_CDN_CACHE_CONTROL]


def test_a_not_modified_timeline_carries_the_same_cache_headers(app_client: ApiClient) -> None:
    first = app_client.get("/api/spots/pipeline/timeline")
    cached = app_client.get(
        "/api/spots/pipeline/timeline", headers={"If-None-Match": first.headers["etag"]}
    )
    assert cached.status_code == 304
    assert cached.headers["etag"] == first.headers["etag"]
    assert cached.headers.get_list("cache-control") == [PUBLIC_GET_CACHE_CONTROL]
    assert cached.headers.get_list("cdn-cache-control") == [PUBLIC_CDN_CACHE_CONTROL]


def test_errors_of_the_public_routes_are_not_cached(app_client: ApiClient) -> None:
    for path in ("/api/spots/no-such-spot/timeline", "/api/spots/no-such-spot/tides"):
        resp = app_client.get(path)
        assert resp.status_code == 404, path
        assert resp.headers.get_list("cache-control") == ["no-store"], path
        assert not shared_cacheable(resp.headers), path
    bad = app_client.get("/api/spots/pipeline/tides?start=2026-01-02T00:00Z&end=2026-01-01T00:00Z")
    assert bad.status_code == 400 and not shared_cacheable(bad.headers)


def test_the_spot_page_routes_are_shared_cacheable(app_client: ApiClient) -> None:
    """The spot detail used to stay out of shared caches because the design expected it to
    depend on the signed-in user (get_optional_user); it never did (the route test below
    checks every route's dependencies), and it, the forecast and the events now come from
    the public snapshot cache with the public headers."""
    for path in SNAPSHOT_GETS:
        resp = app_client.get(path)
        assert resp.status_code == 200, (path, resp.text)
        assert resp.headers.get_list("cache-control") == [PUBLIC_GET_CACHE_CONTROL], path
        assert resp.headers.get_list("cdn-cache-control") == [PUBLIC_CDN_CACHE_CONTROL], path
        assert "set-cookie" not in resp.headers, path
    # The page's one route with a user dependency (the visitor's own opportunities) stays
    # out of shared caches, signed out too.
    own = app_client.get("/api/spots/pipeline/opportunities")
    assert own.status_code == 200 and own.json() == []
    assert own.headers.get_list("cache-control") == ["no-store"]
    assert not shared_cacheable(own.headers)


# --------------------------------------------------------------------------- every route
def _calls(route: APIRoute) -> set[Callable[..., Any]]:
    found: set[Callable[..., Any]] = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            found.add(dep.call)
        stack.extend(dep.dependencies)
    return found


def get_routes() -> list[tuple[str, bool]]:
    """(path, has a user dependency) of every GET route of the app."""
    from app.main import app

    routes: list[tuple[str, bool]] = []
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if isinstance(route, APIRoute) and "GET" in (context.methods or ()):
            assert context.path is not None
            routes.append((context.path, bool(_calls(route) & USER_DEPENDENCIES)))
    return routes


def test_no_route_with_a_user_dependency_is_shared_cacheable(
    app_client: ApiClient, db: Session
) -> None:
    """Signed in as an admin, with a saved search, request every GET route: a route that
    depends on the user (get_auth, get_current_user, get_optional_user, or a dependency built
    on them such as the accuracy routes' require_admin) or a response that sets a cookie never
    carries ``public``, ``s-maxage`` or a CDN header."""
    user = app_client.register()
    db.execute(update(User).where(User.id == user["id"]).values(is_admin=True))
    db.commit()
    created = app_client.post("/api/searches", search_payload())
    assert created.status_code == 201, created.text
    values = {
        "slug": "pipeline",
        "search_id": created.json()["id"],
        "match_id": str(uuid.uuid4()),  # no match exists: the 404 path is checked too
        "event_id": str(uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "variable": "hs",
    }
    routes = get_routes()
    user_routes = {path for path, has_user in routes if has_user}
    assert {"/api/auth/me", "/api/searches", "/api/spots/{slug}/opportunities"} <= user_routes
    seen_public: set[str] = set()
    for path, has_user in routes:
        url = re.sub(r"\{(\w+)(:\w+)?\}", lambda m: values[m.group(1)], path)
        resp = app_client.get(url)
        assert resp.status_code < 500, (path, resp.status_code, resp.text[:300])
        if shared_cacheable(resp.headers):
            seen_public.add(path)
            assert not has_user, (path, dict(resp.headers))
            assert "set-cookie" not in resp.headers, path
    expected_public = {
        "/api/spots",
        "/api/map/conditions",
        "/api/spots/{slug}",
        "/api/spots/{slug}/forecast",
        "/api/spots/{slug}/events",
        "/api/events",
        "/api/spots/{slug}/timeline",
        "/api/spots/{slug}/tides",
        "/api/spots/{slug}/observations",
    }
    assert expected_public <= seen_public, expected_public - seen_public


# --------------------------------------------------------------------------- cookie net
def _cookie_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/public-with-cookie")
    def public_with_cookie(response: Response) -> dict[str, str]:  # a future mistake
        cache_headers(response, browser_seconds=60, cdn_seconds=60, stale_while_revalidate=300)
        response.set_cookie("sta_session", "synthetic")
        return {"ok": "yes"}

    @app.get("/api/shared-with-cookie")
    def shared_with_cookie(response: Response) -> dict[str, str]:  # directives in any case
        response.headers["Cache-Control"] = "S-MaxAge=60"
        response.set_cookie("sta_session", "synthetic")
        return {"ok": "yes"}

    @app.get("/api/public")
    def public(response: Response) -> dict[str, str]:
        cache_headers(response, browser_seconds=60, cdn_seconds=60)
        return {"ok": "yes"}

    @app.get("/api/cookie")
    def cookie(response: Response) -> dict[str, str]:  # like /api/auth/csrf
        response.set_cookie("sta_csrf", "synthetic")
        return {"ok": "yes"}

    app.add_middleware(SecurityHeadersMiddleware)
    return app


def test_a_response_that_sets_a_cookie_is_never_shared_cacheable() -> None:
    client = TestClient(_cookie_app())
    risky = client.get("/api/public-with-cookie")
    assert risky.headers.get_list("cache-control") == ["private, no-store"]
    assert not any(h in risky.headers for h in SHARED_CACHE_HEADERS)
    assert "set-cookie" in risky.headers  # the cookie itself is kept
    shared = client.get("/api/shared-with-cookie")
    assert shared.headers.get_list("cache-control") == ["private, no-store"]
    public = client.get("/api/public")
    assert public.headers["cache-control"] == "public, max-age=60"
    assert public.headers["cdn-cache-control"] == "public, s-maxage=60"
    cookie = client.get("/api/cookie")  # unchanged: the private default
    assert cookie.headers.get_list("cache-control") == ["no-store"]
