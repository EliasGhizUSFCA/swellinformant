"""Authentication, sessions, CSRF, verification, password reset and account deletion."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import SavedSearch, User, UserSession, UserToken
from tests.conftest import ApiClient, latest_link_token, outbox_messages, search_payload

pytestmark = pytest.mark.integration
PASSWORD = "Barrels4Days!"


def test_register_login_logout_me(app_client: ApiClient, db) -> None:  # type: ignore[no-untyped-def]
    user = app_client.register("Kai@Example.com", verify=False)
    assert user["email"] == "kai@example.com"  # normalised
    assert user["email_verified"] is False
    assert app_client.get("/api/auth/me").status_code == 200
    stored = db.scalar(select(User).where(User.email == "kai@example.com"))
    assert stored.password_hash.startswith("$argon2id$") and PASSWORD not in stored.password_hash

    assert app_client.post("/api/auth/logout").status_code == 200
    assert app_client.get("/api/auth/me").status_code == 401
    resp = app_client.post("/api/auth/login", {"email": "kai@example.com", "password": PASSWORD})
    assert resp.status_code == 200 and resp.json()["user"]["full_name"] == "Test Surfer"
    assert app_client.get("/api/auth/me").status_code == 200


def test_session_cookie_flags(app_client: ApiClient) -> None:
    app_client.register(verify=False)
    resp = app_client.post("/api/auth/login", {"email": "surfer@example.com", "password": PASSWORD})
    cookie = next(
        h
        for h in resp.headers.get_list("set-cookie")
        if h.startswith(get_settings().session_cookie_name)
    )
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie


def test_duplicate_email_and_validation(app_client: ApiClient) -> None:
    app_client.register(verify=False)
    dup = app_client.post(
        "/api/auth/register",
        {
            "full_name": "X",
            "email": "surfer@example.com",
            "password": PASSWORD,
            "confirm_password": PASSWORD,
        },
    )
    assert dup.status_code == 409
    mismatch = app_client.post(
        "/api/auth/register",
        {
            "full_name": "X",
            "email": "a@example.com",
            "password": PASSWORD,
            "confirm_password": "nope",
        },
    )
    assert mismatch.status_code == 422 and mismatch.json()["error"]["code"] == "validation_error"
    weak = app_client.post(
        "/api/auth/register",
        {
            "full_name": "X",
            "email": "b@example.com",
            "password": "password123",
            "confirm_password": "password123",
        },
    )
    assert weak.status_code == 400 and weak.json()["error"]["code"] == "weak_password"
    xss = app_client.post(
        "/api/auth/register",
        {
            "full_name": "<script>",
            "email": "c@example.com",
            "password": PASSWORD,
            "confirm_password": PASSWORD,
        },
    )
    assert xss.status_code == 422


def test_wrong_password_and_lockout(app_client: ApiClient, db) -> None:  # type: ignore[no-untyped-def]
    app_client.register(verify=False)
    app_client.post("/api/auth/logout")
    for _ in range(get_settings().login_max_failures):
        r = app_client.post(
            "/api/auth/login", {"email": "surfer@example.com", "password": "wrong-password-1"}
        )
        assert r.status_code == 401
    locked = app_client.post(
        "/api/auth/login", {"email": "surfer@example.com", "password": PASSWORD}
    )
    assert locked.status_code == 401 and "Too many" in locked.json()["error"]["message"]
    unknown = app_client.post(
        "/api/auth/login", {"email": "nobody@example.com", "password": PASSWORD}
    )
    assert (
        unknown.status_code == 401
        and unknown.json()["error"]["message"] == "Invalid email or password."
    )


def test_csrf_is_required_for_state_changes(app_client: ApiClient) -> None:
    app_client.register(verify=False)
    raw = app_client.client
    assert raw.post("/api/searches", json=search_payload()).status_code == 403
    assert (
        raw.post(
            "/api/searches", json=search_payload(), headers={"X-CSRF-Token": "forged"}
        ).status_code
        == 403
    )
    assert app_client.post("/api/searches", search_payload()).status_code == 201


def test_unauthenticated_requests_are_rejected(app_client: ApiClient) -> None:
    for method, url in [
        ("get", "/api/searches"),
        ("get", "/api/opportunities"),
        ("get", "/api/notifications"),
        ("get", "/api/auth/me"),
        ("post", "/api/searches"),
        ("patch", "/api/users/me/profile"),
    ]:
        resp = (
            getattr(app_client, method)(url)
            if method == "get"
            else getattr(app_client, method)(url, {})
        )
        assert resp.status_code == 401, (method, url, resp.status_code)


def test_email_verification_single_use_and_expiry(app_client: ApiClient, db) -> None:  # type: ignore[no-untyped-def]
    app_client.register(verify=False)
    token = latest_link_token("verify-email")
    assert app_client.post("/api/auth/verify-email", {"token": token}).status_code == 200
    assert app_client.get("/api/auth/me").json()["email_verified"] is True
    assert app_client.post("/api/auth/verify-email", {"token": token}).status_code == 400

    app_client.post("/api/auth/logout")
    app_client.register("second@example.com", verify=False)
    token2 = latest_link_token("verify-email")
    db.execute(update(UserToken).values(expires_at=utcnow() - timedelta(minutes=1)))
    db.commit()
    assert app_client.post("/api/auth/verify-email", {"token": token2}).status_code == 400


def test_password_reset_flow(make_client) -> None:  # type: ignore[no-untyped-def]
    device_a: ApiClient = make_client()
    device_a.register()
    device_b: ApiClient = make_client()
    assert (
        device_b.post(
            "/api/auth/login", {"email": "surfer@example.com", "password": PASSWORD}
        ).status_code
        == 200
    )

    anon: ApiClient = make_client()
    msg = anon.post("/api/auth/forgot-password", {"email": "surfer@example.com"})
    assert msg.status_code == 200
    unknown = anon.post("/api/auth/forgot-password", {"email": "nobody@example.com"})
    assert unknown.json() == msg.json()  # no account enumeration
    token = latest_link_token("reset-password")
    new = "NewSwell2026!"
    assert (
        anon.post(
            "/api/auth/reset-password", {"token": token, "password": new, "confirm_password": new}
        ).status_code
        == 200
    )
    # token is single-use
    assert (
        anon.post(
            "/api/auth/reset-password", {"token": token, "password": new, "confirm_password": new}
        ).status_code
        == 400
    )
    # every existing session was revoked
    assert device_a.get("/api/auth/me").status_code == 401
    assert device_b.get("/api/auth/me").status_code == 401
    assert (
        anon.post(
            "/api/auth/login", {"email": "surfer@example.com", "password": PASSWORD}
        ).status_code
        == 401
    )
    assert (
        anon.post("/api/auth/login", {"email": "surfer@example.com", "password": new}).status_code
        == 200
    )


def test_password_reset_token_expires(app_client: ApiClient, db) -> None:  # type: ignore[no-untyped-def]
    app_client.register(verify=False)
    app_client.post("/api/auth/forgot-password", {"email": "surfer@example.com"})
    token = latest_link_token("reset-password")
    db.execute(
        update(UserToken)
        .where(UserToken.purpose == "password_reset")
        .values(expires_at=utcnow() - timedelta(seconds=1))
    )
    db.commit()
    r = app_client.post(
        "/api/auth/reset-password",
        {"token": token, "password": "NewSwell2026!", "confirm_password": "NewSwell2026!"},
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_token"


def test_change_password_keeps_current_session_only(make_client) -> None:  # type: ignore[no-untyped-def]
    a: ApiClient = make_client()
    a.register()
    b: ApiClient = make_client()
    b.post("/api/auth/login", {"email": "surfer@example.com", "password": PASSWORD})
    bad = a.post(
        "/api/auth/change-password",
        {"current_password": "nope", "new_password": "x", "confirm_password": "x"},
    )
    assert bad.status_code == 400
    ok = a.post(
        "/api/auth/change-password",
        {
            "current_password": PASSWORD,
            "new_password": "Another1Swell!",
            "confirm_password": "Another1Swell!",
        },
    )
    assert ok.status_code == 200
    assert a.get("/api/auth/me").status_code == 200
    assert b.get("/api/auth/me").status_code == 401


def test_account_deletion_removes_all_data(app_client: ApiClient, db) -> None:  # type: ignore[no-untyped-def]
    app_client.register()
    assert app_client.post("/api/searches", search_payload()).status_code == 201
    assert (
        app_client.delete("/api/users/me", {"password": "wrong", "confirm": "DELETE"}).status_code
        == 400
    )
    assert (
        app_client.delete("/api/users/me", {"password": PASSWORD, "confirm": "DELETE"}).status_code
        == 200
    )
    assert app_client.get("/api/auth/me").status_code == 401
    assert db.scalar(select(func.count(User.id))) == 0
    assert db.scalar(select(func.count(SavedSearch.id))) == 0
    assert db.scalar(select(func.count(UserSession.id))) == 0


def test_rate_limiting(app_client: ApiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    codes = [
        app_client.post(
            "/api/auth/login", {"email": "x@example.com", "password": "Whatever123!"}
        ).status_code
        for _ in range(12)
    ]
    assert 429 in codes
    limited = app_client.post(
        "/api/auth/login", {"email": "x@example.com", "password": "Whatever123!"}
    )
    assert limited.status_code == 429 and "Retry-After" in limited.headers


def test_security_headers_and_safe_errors(app_client: ApiClient) -> None:
    resp = app_client.get("/api/health")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    missing = app_client.get("/api/spots/does-not-exist")
    assert missing.status_code == 404 and set(missing.json()) == {"error"}


def test_verification_email_is_sent(app_client: ApiClient) -> None:
    app_client.register(verify=False)
    assert any("Verify your email" in str(m.get("subject")) for m in outbox_messages())
