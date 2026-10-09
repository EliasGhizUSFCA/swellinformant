"""Account lifecycle: registration, login, verification, password reset, phone, deletion."""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth.security import (
    DUMMY_HASH,
    PasswordPolicyError,
    hash_password,
    hash_token,
    needs_rehash,
    new_numeric_code,
    new_token,
    validate_password,
    verify_password,
)
from app.auth.sessions import revoke_all_sessions
from app.core.config import get_settings
from app.core.database import utcnow
from app.core.errors import AppError, Conflict, Unauthorized
from app.models import NotificationPreference, User, UserProfile, UserToken
from app.models.enums import TokenPurpose
from app.services.notifications.templates import (
    render_password_reset_email,
    render_verification_email,
)
from app.workers.dispatch import dispatch_email, dispatch_sms

logger = logging.getLogger(__name__)

E164 = re.compile(r"^\+[1-9]\d{6,14}$")
PHONE_CODE_TTL = timedelta(minutes=10)
PHONE_MAX_ATTEMPTS = 5


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _policy(password: str, email: str) -> None:
    try:
        validate_password(password, email)
    except PasswordPolicyError as exc:
        raise AppError(str(exc), code="weak_password") from exc


def issue_token(db: Session, user: User, purpose: TokenPurpose, ttl: timedelta) -> str:
    now = utcnow()
    db.execute(
        update(UserToken)
        .where(
            UserToken.user_id == user.id, UserToken.purpose == purpose, UserToken.used_at.is_(None)
        )
        .values(used_at=now)
    )
    token = new_token(32)
    db.add(
        UserToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=hash_token(token),
            created_at=now,
            expires_at=now + ttl,
        )
    )
    return token


def consume_token(db: Session, token: str, purpose: TokenPurpose) -> User:
    now = utcnow()
    row = db.scalar(
        select(UserToken).where(
            UserToken.token_hash == hash_token(token), UserToken.purpose == purpose
        )
    )
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise AppError(
            "This link is invalid or has expired. Please request a new one.", code="invalid_token"
        )
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise AppError(
            "This link is invalid or has expired. Please request a new one.", code="invalid_token"
        )
    row.used_at = now
    return user


def send_verification(db: Session, user: User) -> None:
    s = get_settings()
    token = issue_token(
        db, user, TokenPurpose.EMAIL_VERIFY, timedelta(hours=s.email_verification_ttl_hours)
    )
    db.commit()
    subject, text, html = render_verification_email(
        user.full_name, f"{s.app_base_url}/verify-email?token={token}"
    )
    dispatch_email(user.email, subject, text, html, tag="verify_email")


def register(db: Session, full_name: str, email: str, password: str) -> User:
    email = normalize_email(email)
    _policy(password, email)
    if db.scalar(select(User.id).where(User.email == email)):
        raise Conflict("An account with this email already exists. Try signing in instead.")
    user = User(
        email=email,
        full_name=full_name.strip(),
        password_hash=hash_password(password),
        password_changed_at=utcnow(),
    )
    user.profile = UserProfile()
    user.notification_preferences = NotificationPreference(unsubscribe_token=new_token(24))
    db.add(user)
    db.commit()
    send_verification(db, user)
    return user


def authenticate(db: Session, email: str, password: str) -> User:
    s = get_settings()
    now = utcnow()
    user = db.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None:
        verify_password(DUMMY_HASH, password)  # equalise timing to avoid account enumeration
        raise Unauthorized("Invalid email or password.")
    if user.locked_until and user.locked_until > now:
        verify_password(DUMMY_HASH, password)
        raise Unauthorized(
            "Too many failed attempts. Try again in a few minutes or reset your password."
        )
    if not user.is_active or not verify_password(user.password_hash, password):
        user.failed_login_count += 1
        if user.failed_login_count >= s.login_max_failures:
            user.locked_until = now + timedelta(minutes=s.login_lockout_minutes)
            user.failed_login_count = 0
        db.commit()
        raise Unauthorized("Invalid email or password.")
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    return user


def verify_email(db: Session, token: str) -> User:
    user = consume_token(db, token, TokenPurpose.EMAIL_VERIFY)
    if user.email_verified_at is None:
        user.email_verified_at = utcnow()
    db.commit()
    return user


def request_password_reset(db: Session, email: str) -> None:
    """Always succeeds silently so the endpoint cannot reveal which emails exist."""
    s = get_settings()
    user = db.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None or not user.is_active:
        return
    token = issue_token(
        db, user, TokenPurpose.PASSWORD_RESET, timedelta(minutes=s.password_reset_ttl_minutes)
    )
    db.commit()
    subject, text, html = render_password_reset_email(
        user.full_name,
        f"{s.app_base_url}/reset-password?token={token}",
        s.password_reset_ttl_minutes,
    )
    dispatch_email(user.email, subject, text, html, tag="password_reset")


def reset_password(db: Session, token: str, new_password: str) -> User:
    user = consume_token(db, token, TokenPurpose.PASSWORD_RESET)
    _policy(new_password, user.email)
    user.password_hash = hash_password(new_password)
    user.password_changed_at = utcnow()
    user.failed_login_count = 0
    user.locked_until = None
    revoke_all_sessions(db, user.id)
    db.commit()
    return user


def change_password(
    db: Session, user: User, current: str, new: str, keep_session_id: object
) -> None:
    if not verify_password(user.password_hash, current):
        raise AppError("Current password is incorrect.", code="invalid_password")
    _policy(new, user.email)
    user.password_hash = hash_password(new)
    user.password_changed_at = utcnow()
    revoke_all_sessions(db, user.id, keep_session_id=keep_session_id)
    db.commit()


def delete_account(db: Session, user: User, password: str) -> None:
    """Hard delete: every search, match, notification and session cascades with the user."""
    if not verify_password(user.password_hash, password):
        raise AppError("Password is incorrect.", code="invalid_password")
    db.delete(user)
    db.commit()
    logger.info("Account %s deleted", user.id)


def start_phone_verification(db: Session, user: User, phone: str) -> None:
    phone = re.sub(r"[\s().-]", "", phone)
    if not E164.match(phone):
        raise AppError(
            "Enter the phone number in international format, e.g. +14155550123.",
            code="invalid_phone",
        )
    prefs = user.notification_preferences
    code = new_numeric_code(6)
    prefs.phone_number = phone
    prefs.phone_verified_at = None
    prefs.sms_opt_in_at = None
    prefs.phone_verification_code_hash = hash_token(f"{user.id}:{code}")
    prefs.phone_verification_expires_at = utcnow() + PHONE_CODE_TTL
    prefs.phone_verification_attempts = 0
    db.commit()
    dispatch_sms(
        phone, f"Your Swell Travel Agent verification code is {code}. It expires in 10 minutes."
    )


def confirm_phone(db: Session, user: User, code: str) -> NotificationPreference:
    prefs = user.notification_preferences
    now = utcnow()
    if not prefs.phone_verification_code_hash or not prefs.phone_verification_expires_at:
        raise AppError("Request a verification code first.", code="no_code")
    if (
        prefs.phone_verification_expires_at <= now
        or prefs.phone_verification_attempts >= PHONE_MAX_ATTEMPTS
    ):
        raise AppError("This code has expired. Request a new one.", code="code_expired")
    prefs.phone_verification_attempts += 1
    if hash_token(f"{user.id}:{code.strip()}") != prefs.phone_verification_code_hash:
        db.commit()
        raise AppError("That code is not correct.", code="invalid_code")
    prefs.phone_verified_at = now
    prefs.sms_opt_in_at = now  # explicit opt-in: the user requested and confirmed the code
    prefs.sms_opted_out_at = None
    prefs.sms_enabled = True
    prefs.phone_verification_code_hash = None
    prefs.phone_verification_expires_at = None
    db.commit()
    return prefs
