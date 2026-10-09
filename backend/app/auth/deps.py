"""FastAPI dependencies for authentication and authorisation."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth.sessions import resolve_session
from app.core.config import get_settings
from app.core.database import get_db
from app.core.errors import Forbidden, Unauthorized
from app.models import User, UserSession


@dataclass
class AuthContext:
    user: User
    session: UserSession
    token: str


def _token(request: Request) -> str | None:
    return request.cookies.get(get_settings().session_cookie_name)


def get_auth(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    token = _token(request)
    resolved = resolve_session(db, token)
    if resolved is None or token is None:
        raise Unauthorized("Please sign in to continue.")
    user, session = resolved
    return AuthContext(user=user, session=session, token=token)


def get_current_user(auth: AuthContext = Depends(get_auth)) -> User:
    return auth.user


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    resolved = resolve_session(db, _token(request))
    return resolved[0] if resolved else None


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise Forbidden("Administrator access required.")
    return user
