"""Password hashing (Argon2id), password policy and opaque token helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# OWASP-recommended Argon2id parameters (m=64 MiB, t=3, p=4).
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128
COMMON_PASSWORDS = {
    "password",
    "password1",
    "password123",
    "1234567890",
    "12345678910",
    "qwertyuiop",
    "letmein123",
    "iloveyou12",
    "surfing123",
    "welcome123",
    "abcdefghij",
    "0123456789",
}


class PasswordPolicyError(ValueError):
    pass


def validate_password(password: str, email: str | None = None) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")
    if password.lower() in COMMON_PASSWORDS or len(set(password)) < 4:
        raise PasswordPolicyError("Password is too common or too simple.")
    if not any(c.isalpha() for c in password) or not any(not c.isalpha() for c in password):
        raise PasswordPolicyError(
            "Password must contain letters and at least one number or symbol."
        )
    if email and email.split("@")[0].lower() in password.lower() and len(email.split("@")[0]) >= 4:
        raise PasswordPolicyError("Password must not contain your email name.")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


# A valid hash computed once, used to equalise timing when the email does not exist.
DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-real-password")


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def new_numeric_code(digits: int = 6) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(digits))
