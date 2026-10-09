"""Profile, notification preferences, phone verification, saved airports and searches."""

from __future__ import annotations

import re

import pytest

from tests.conftest import ApiClient, outbox_messages, search_payload

pytestmark = pytest.mark.integration


def test_profile_and_saved_airports(app_client: ApiClient) -> None:
    app_client.register()
    r = app_client.patch(
        "/api/users/me/profile",
        {
            "home_airport": "sfo",
            "home_city": "San Francisco",
            "preferred_wave_min_ft": 6,
            "preferred_wave_max_ft": 15,
            "experience_level": "advanced",
            "units": "m",
        },
    )
    assert r.status_code == 200, r.text
    profile = r.json()["profile"]
    assert profile["home_airport"] == "SFO" and profile["units"] == "m"
    assert app_client.patch("/api/users/me/profile", {"home_airport": "ZZZ"}).status_code == 400
    assert (
        app_client.patch(
            "/api/users/me/profile", {"preferred_wave_min_ft": 20, "preferred_wave_max_ft": 5}
        ).status_code
        == 422
    )

    added = app_client.post(
        "/api/users/me/airports", {"airport_iata": "OAK", "label": "Backup", "is_home": False}
    )
    assert added.status_code == 201 and added.json()[0]["airport_iata"] == "OAK"
    assert app_client.post("/api/users/me/airports", {"airport_iata": "QQQ"}).status_code == 400
    airport_id = added.json()[0]["id"]
    assert app_client.delete(f"/api/users/me/airports/{airport_id}").json() == []


def test_notification_preferences_and_unsubscribe(app_client: ApiClient) -> None:
    app_client.register()
    r = app_client.patch(
        "/api/users/me/notification-preferences", {"all_paused": True, "max_alerts_per_day": 3}
    )
    assert r.json()["notification_preferences"]["all_paused"] is True
    assert (
        app_client.patch(
            "/api/users/me/notification-preferences", {"sms_enabled": True}
        ).status_code
        == 400
    )

    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.models import NotificationPreference

    with SessionLocal() as s:
        token = s.scalar(select(NotificationPreference.unsubscribe_token))
    # No session and no CSRF needed: the secret token is the authorisation.
    anon = app_client.client
    raw = anon.post(
        f"/api/notifications/unsubscribe?token={token}", data={"List-Unsubscribe": "One-Click"}
    )
    assert raw.status_code == 200
    assert (
        app_client.get("/api/auth/me").json()["notification_preferences"]["email_enabled"] is False
    )
    assert (
        anon.post("/api/notifications/unsubscribe", json={"token": "bogus-token-123"}).status_code
        == 400
    )


def test_phone_verification_flow(app_client: ApiClient) -> None:
    app_client.register()
    assert (
        app_client.post("/api/users/me/phone/start", {"phone_number": "4155550123"}).status_code
        == 400
    )
    assert (
        app_client.post(
            "/api/users/me/phone/start", {"phone_number": "+1 (415) 555-0123"}
        ).status_code
        == 200
    )
    sms = next(m for m in outbox_messages() if m["kind"] == "sms")
    assert sms["to"] == "+14155550123"
    code = re.search(r"\b(\d{6})\b", str(sms["text"])).group(1)  # type: ignore[union-attr]
    assert (
        app_client.post(
            "/api/users/me/phone/confirm", {"code": "000000" if code != "000000" else "111111"}
        ).status_code
        == 400
    )
    ok = app_client.post("/api/users/me/phone/confirm", {"code": code})
    prefs = ok.json()["notification_preferences"]
    assert prefs["phone_verified"] and prefs["sms_ready"] and prefs["sms_enabled"]
    out = app_client.post("/api/users/me/sms/opt-out").json()["notification_preferences"]
    assert out["sms_ready"] is False and out["sms_opted_out"] is True


def test_search_crud_and_validation(app_client: ApiClient) -> None:
    app_client.register()
    created = app_client.post(
        "/api/searches", search_payload(units="m", wave_min=1.5, wave_max=4.5)
    )
    assert created.status_code == 201, created.text
    s = created.json()
    assert s["wave_min_ft"] == pytest.approx(4.92, abs=0.01) and s["wave_min"] == 1.5
    assert s["destinations"]["spot_slugs"] == ["jeffreys-bay"] and s["origins"] == ["SFO"]
    sid = s["id"]

    bad_cases = [
        search_payload(wave_min=10, wave_max=5),
        search_payload(origins=[]),
        search_payload(date_mode="fixed"),
        search_payload(destination_mode="spots", destinations={"spot_slugs": []}),
        search_payload(travel={"max_price": -1}),
        search_payload(min_quality="poor"),
    ]
    for payload in bad_cases:
        assert app_client.post("/api/searches", payload).status_code == 422
    assert app_client.post("/api/searches", search_payload(origins=["ZZZ"])).status_code == 400
    assert (
        app_client.post(
            "/api/searches", search_payload(destinations={"spot_slugs": ["atlantis"]})
        ).status_code
        == 400
    )

    updated = app_client.put(
        f"/api/searches/{sid}",
        search_payload(
            name="Renamed",
            destination_mode="regions",
            destinations={"region_groups": ["Africa"], "countries": ["PT"]},
        ),
    )
    assert updated.status_code == 200 and updated.json()["destinations"]["region_groups"] == [
        "Africa"
    ]
    assert updated.json()["destinations"]["countries"] == ["PT"]
    assert app_client.post(f"/api/searches/{sid}/pause").json()["status"] == "paused"
    assert app_client.post(f"/api/searches/{sid}/resume").json()["status"] == "active"
    copy = app_client.post(f"/api/searches/{sid}/duplicate")
    assert (
        copy.status_code == 201
        and copy.json()["status"] == "paused"
        and copy.json()["name"].endswith("(copy)")
    )
    assert len(app_client.get("/api/searches").json()) == 2
    assert app_client.get(f"/api/searches/{sid}/matches").status_code == 200
    assert app_client.delete(f"/api/searches/{sid}").status_code == 200
    assert app_client.get(f"/api/searches/{sid}").status_code == 404


def test_fixed_date_search(app_client: ApiClient) -> None:
    from datetime import date, timedelta

    app_client.register()
    start = date.today() + timedelta(days=3)
    r = app_client.post(
        "/api/searches",
        search_payload(
            date_mode="fixed",
            date_start=start.isoformat(),
            date_end=(start + timedelta(days=20)).isoformat(),
        ),
    )
    assert r.status_code == 201 and r.json()["date_mode"] == "fixed"
    past = app_client.post(
        "/api/searches",
        search_payload(date_mode="fixed", date_start="2020-01-01", date_end="2020-01-10"),
    )
    assert past.status_code == 422


def test_users_cannot_access_each_others_data(make_client) -> None:  # type: ignore[no-untyped-def]
    alice: ApiClient = make_client()
    alice.register("alice@example.com")
    sid = alice.post("/api/searches", search_payload()).json()["id"]
    bob: ApiClient = make_client()
    bob.register("bob@example.com")
    assert bob.get(f"/api/searches/{sid}").status_code == 404
    assert bob.put(f"/api/searches/{sid}", search_payload()).status_code == 404
    assert bob.post(f"/api/searches/{sid}/pause").status_code == 404
    assert bob.delete(f"/api/searches/{sid}").status_code == 404
    assert bob.get(f"/api/searches/{sid}/matches").status_code == 404
    assert bob.get("/api/searches").json() == []
    assert alice.get(f"/api/searches/{sid}").status_code == 200
