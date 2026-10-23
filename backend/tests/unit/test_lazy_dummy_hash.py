"""The login's timing-equaliser hash is computed on first use, not at import.

Hashing it at import cost about 250 ms of every cold start of the API (api-serverless's
-X importtime measurement). It must still be a real Argon2id hash that an unknown email
is verified against, and the first verification computes it whichever path comes first.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.auth import security

BACKEND = Path(__file__).resolve().parents[2]


def test_importing_the_module_computes_no_hash() -> None:
    code = "import app.auth.security as s\nprint(s.dummy_hash.cache_info().currsize)\n"
    out = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "0"


class RecordingHasher:
    """Delegates to the real PasswordHasher and records the hashes it verifies against."""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.seen: list[str] = []

    def hash(self, password: str) -> str:
        return str(self.real.hash(password))

    def verify(self, password_hash: str, password: str) -> bool:
        self.seen.append(password_hash)
        return bool(self.real.verify(password_hash, password))


def test_an_unknown_email_is_verified_against_a_real_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = RecordingHasher(security._hasher)
    monkeypatch.setattr(security, "_hasher", recording)
    assert security.verify_password(security.DUMMY_HASH, "not the password 1") is False
    assert recording.seen == [security.dummy_hash()]
    assert recording.seen[0].startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert recording.seen[0] != security.DUMMY_HASH


def test_the_first_verification_computes_it_whichever_path_comes_first() -> None:
    security.dummy_hash.cache_clear()
    known = security.hash_password("synthetic password 1")
    assert security.dummy_hash.cache_info().currsize == 0
    assert security.verify_password(known, "synthetic password 1") is True
    assert security.dummy_hash.cache_info().currsize == 1  # a known email paid for it too
