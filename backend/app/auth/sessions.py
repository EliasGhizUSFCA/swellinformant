"""Server-side sessions in HttpOnly cookies, plus double-submit CSRF tokens."""

from __future__ import annotations

from datetime import timedelta

from fastapi import Request, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth.security import hash_token, new_token
from app.core.config import get_settings
from app.core.database import utcnow
from app.core.ratelimit import client_ip
from app.models import User, UserSession

TOUCH_INTERVAL = timedelta(minutes=5)


def create_session(db: Session, user: User, request: Request) -> str:
    settings = get_settings()
    token = new_token(32)
    now = utcnow()
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_token(token),
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(days=settings.session_ttl_days),
            user_agent=(request.headers.get("user-agent") or "")[:255],
            ip_address=client_ip(request)[:64],
        )
    )
    return token


def resolve_session(db: Session, token: str | None) -> tuple[User, UserSession] | None:
    if not token or len(token) > 200:
        return None
    now = utcnow()
    session = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
    if session is None or session.revoked_at is not None or session.expires_at <= now:
        return None
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        return None
    if now - session.last_seen_at > TOUCH_INTERVAL:
        session.last_seen_at = now
        # Sliding expiry: active sessions stay alive, idle ones expire after the TTL.
        session.expires_at = now + timedelta(days=get_settings().session_ttl_days)
        db.commit()
    return user, session


def revoke_session(db: Session, token: str | None) -> None:
    if not token:
        return
    db.execute(
        update(UserSession)
        .where(UserSession.token_hash == hash_token(token), UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


def revoke_all_sessions(
    db: Session, user_id: object, keep_session_id: object | None = None
) -> None:
    stmt = update(UserSession).where(
        UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
    )
    if keep_session_id is not None:
        stmt = stmt.where(UserSession.id != keep_session_id)
    db.execute(stmt.values(revoked_at=utcnow()))


def set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        s.session_cookie_name,
        token,
        max_age=s.session_ttl_days * 86400,
        httponly=True,
        secure=s.cookie_secure,
        samesite="lax",
        domain=s.cookie_domain,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(
        s.session_cookie_name,
        path="/",
        domain=s.cookie_domain,
        secure=s.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def issue_csrf_cookie(response: Response, existing: str | None = None) -> str:
    """Set (or refresh) the readable CSRF cookie the frontend echoes in X-CSRF-Token."""
    s = get_settings()
    token = existing or new_token(24)
    response.set_cookie(
        s.csrf_cookie_name,
        token,
        max_age=s.session_ttl_days * 86400,
        httponly=False,
        secure=s.cookie_secure,
        samesite="lax",
        domain=s.cookie_domain,
        path="/",
    )
    return token
