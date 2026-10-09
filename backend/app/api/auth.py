"""Registration, login, logout, email verification and password reset."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.api.presenters import user_out
from app.auth.deps import AuthContext, get_auth
from app.auth.sessions import (
    clear_session_cookie,
    create_session,
    issue_csrf_cookie,
    revoke_session,
    set_session_cookie,
)
from app.core.config import get_settings
from app.core.database import get_db
from app.core.ratelimit import client_ip, hit, rate_limit
from app.schemas.auth import (
    AuthOut,
    ChangePasswordIn,
    ForgotPasswordIn,
    LoginIn,
    RegisterIn,
    ResetPasswordIn,
    TokenIn,
    UserOut,
)
from app.schemas.common import Message
from app.services import accounts

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _login_response(db: Session, request: Request, response: Response, user: object) -> AuthOut:
    token = create_session(db, user, request)  # type: ignore[arg-type]
    db.commit()
    set_session_cookie(response, token)
    csrf = issue_csrf_cookie(response)  # rotate the CSRF token on every login
    return AuthOut(user=user_out(db, user), csrf_token=csrf)  # type: ignore[arg-type]


@router.get("/csrf")
def csrf(request: Request, response: Response) -> dict[str, str]:
    """Issue the CSRF cookie (call once before the first POST)."""
    existing = request.cookies.get(get_settings().csrf_cookie_name)
    return {"csrf_token": issue_csrf_cookie(response, existing)}


@router.post(
    "/register",
    response_model=AuthOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("register", 20, 3600))],
)
def register(
    body: RegisterIn, request: Request, response: Response, db: Session = Depends(get_db)
) -> AuthOut:
    user = accounts.register(db, body.full_name, body.email, body.password)
    return _login_response(db, request, response, user)


@router.post("/login", response_model=AuthOut, dependencies=[Depends(rate_limit("login", 30, 300))])
def login(
    body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)
) -> AuthOut:
    hit("login-email", accounts.normalize_email(body.email), 10, 900)
    user = accounts.authenticate(db, body.email, body.password)
    return _login_response(db, request, response, user)


@router.post("/logout", response_model=Message)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> Message:
    revoke_session(db, request.cookies.get(get_settings().session_cookie_name))
    db.commit()
    clear_session_cookie(response)
    return Message(message="Signed out.")


@router.get("/me", response_model=UserOut)
def me(auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)) -> UserOut:
    return user_out(db, auth.user)


@router.post(
    "/verify-email", response_model=Message, dependencies=[Depends(rate_limit("verify", 30, 900))]
)
def verify_email(body: TokenIn, db: Session = Depends(get_db)) -> Message:
    accounts.verify_email(db, body.token)
    return Message(message="Email verified. You'll now receive swell alerts by email.")


@router.post("/resend-verification", response_model=Message)
def resend_verification(
    request: Request, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)
) -> Message:
    hit("resend-verification", str(auth.user.id), 5, 3600)
    if auth.user.email_verified_at is None:
        accounts.send_verification(db, auth.user)
    return Message(message="If your email still needs verifying, a new link is on its way.")


@router.post(
    "/forgot-password",
    response_model=Message,
    dependencies=[Depends(rate_limit("forgot-password", 10, 900))],
)
def forgot_password(
    body: ForgotPasswordIn, request: Request, db: Session = Depends(get_db)
) -> Message:
    hit(
        "forgot-password-email",
        f"{accounts.normalize_email(body.email)}:{client_ip(request)}",
        3,
        900,
    )
    accounts.request_password_reset(db, body.email)
    return Message(message="If an account exists for that email, a reset link has been sent.")


@router.post(
    "/reset-password",
    response_model=Message,
    dependencies=[Depends(rate_limit("reset-password", 20, 900))],
)
def reset_password(
    body: ResetPasswordIn, response: Response, db: Session = Depends(get_db)
) -> Message:
    accounts.reset_password(db, body.token, body.password)
    clear_session_cookie(response)
    return Message(message="Password updated. Please sign in with your new password.")


@router.post("/change-password", response_model=Message)
def change_password(
    body: ChangePasswordIn, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)
) -> Message:
    hit("change-password", str(auth.user.id), 10, 900)
    accounts.change_password(
        db, auth.user, body.current_password, body.new_password, auth.session.id
    )
    return Message(message="Password changed. Other devices have been signed out.")
