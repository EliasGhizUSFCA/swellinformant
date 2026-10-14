"""The spot timeline endpoint: contract shape, explicit "unavailable" state, 404."""

from __future__ import annotations

import pytest

from app.schemas.timeline import SpotTimelineOut
from tests.conftest import ApiClient

pytestmark = pytest.mark.integration


def test_timeline_says_it_is_not_available_yet(app_client: ApiClient) -> None:
    resp = app_client.get("/api/spots/jeffreys-bay/timeline", params={"days": 3})
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "public, max-age=300"
    body = SpotTimelineOut.model_validate(resp.json())
    assert body.spot_slug == "jeffreys-bay" and body.timezone == "Africa/Johannesburg"
    assert body.available is False and body.message == "Timeline not available yet"
    assert body.steps == [] and body.days == [] and body.provenance is None
    assert body.steps_meta.segments == [] and body.steps_meta.interpolation == "none"
    assert body.units.model_dump() == {
        "breaking": "ft",
        "offshore": "m",
        "wind": "km/h",
        "tide": "m",
    }
    # The embedded blocks state their own status; nothing is made up.
    assert body.tide.status == "unavailable" and body.tide.curve == []
    assert body.observations.status == "unavailable" and body.observations.items == []
    assert body.generated_at.tzinfo is not None


def test_timeline_validates_days_and_slug(app_client: ApiClient) -> None:
    assert app_client.get("/api/spots/jeffreys-bay/timeline?days=0").status_code == 422
    assert app_client.get("/api/spots/jeffreys-bay/timeline?days=17").status_code == 422
    missing = app_client.get("/api/spots/no-such-spot/timeline")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
