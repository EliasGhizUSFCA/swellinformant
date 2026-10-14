"""Tide and observation endpoints: the contract shape and the explicit "unavailable" state."""

from __future__ import annotations

import pytest

from app.schemas.tides import TIDE_PREFERENCE_NOT_ESTABLISHED, ObservationsBlockOut, TideBlockOut
from tests.conftest import ApiClient

pytestmark = pytest.mark.integration


def test_tides_are_unavailable_rather_than_invented(app_client: ApiClient) -> None:
    resp = app_client.get("/api/spots/mavericks/tides")
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "public, max-age=300"
    block = TideBlockOut.model_validate(resp.json())
    assert block.status == "unavailable"
    assert block.reason == "No tide source configured for this spot."
    assert block.curve == [] and block.events == [] and block.station is None
    assert block.datum is None and block.source is None and block.units == "m"
    assert block.preference.text == TIDE_PREFERENCE_NOT_ESTABLISHED

    window = app_client.get(
        "/api/spots/mavericks/tides",
        params={"start": "2026-10-14T00:00:00Z", "end": "2026-10-16T00:00:00Z"},
    )
    assert window.status_code == 200 and window.json()["status"] == "unavailable"
    backwards = app_client.get(
        "/api/spots/mavericks/tides",
        params={"start": "2026-10-16T00:00:00Z", "end": "2026-10-14T00:00:00Z"},
    )
    assert backwards.status_code == 400


def test_observations_are_unavailable_rather_than_invented(app_client: ApiClient) -> None:
    resp = app_client.get("/api/spots/rincon/observations")
    assert resp.status_code == 200, resp.text
    block = ObservationsBlockOut.model_validate(resp.json())
    assert block.status == "unavailable" and block.items == [] and block.reason


@pytest.mark.parametrize("path", ["tides", "observations"])
def test_unknown_spot_is_404(app_client: ApiClient, path: str) -> None:
    resp = app_client.get(f"/api/spots/no-such-spot/{path}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
