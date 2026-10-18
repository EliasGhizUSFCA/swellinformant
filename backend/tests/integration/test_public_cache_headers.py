"""Cache headers of the user-independent public GETs. Owned by the api-serverless package.

Created by the foundation with the headers the routes send today. The constants below are
also what the route tests of other files assert (test_tides_api, test_spot_observations_api,
test_timeline_api, test_public_and_opportunity_api, test_map_fields), so when api-serverless
adds the CDN headers (app.core.http_cache.cache_headers) it updates the expected values here,
in a file it owns, and those tests follow.
"""

from __future__ import annotations

import pytest

from tests.conftest import ApiClient

pytestmark = pytest.mark.integration

# /api/map/conditions, /api/spots/{slug}/timeline, /tides and /observations.
PUBLIC_GET_CACHE_CONTROL = "public, max-age=300"
# /api/spots: no header of its own today, so the security middleware's default applies.
SPOT_LIST_CACHE_CONTROL = "no-store"
# Routes with a user dependency (get_optional_user): never cacheable by a shared cache.
USER_ROUTE_CACHE_CONTROL = "no-store"

PUBLIC_GETS = (
    "/api/map/conditions",
    "/api/spots/pipeline/timeline",
    "/api/spots/pipeline/tides",
    "/api/spots/pipeline/observations",
)


def test_public_gets_send_today_s_cache_headers(app_client: ApiClient) -> None:
    for path in PUBLIC_GETS:
        resp = app_client.get(path)
        assert resp.status_code == 200, (path, resp.text)
        assert resp.headers.get_list("cache-control") == [PUBLIC_GET_CACHE_CONTROL], path
        assert "cdn-cache-control" not in resp.headers, path
    spots = app_client.get("/api/spots")
    assert spots.status_code == 200
    assert spots.headers.get_list("cache-control") == [SPOT_LIST_CACHE_CONTROL]


def test_a_route_with_a_user_dependency_is_never_shared_cacheable(
    app_client: ApiClient,
) -> None:
    resp = app_client.get("/api/spots/pipeline")  # get_optional_user
    assert resp.status_code == 200
    assert resp.headers.get_list("cache-control") == [USER_ROUTE_CACHE_CONTROL]
    assert "cdn-cache-control" not in resp.headers
