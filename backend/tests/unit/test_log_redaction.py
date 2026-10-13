"""Log redaction beyond the message itself: exception text and uvicorn's own loggers.

Seen on a real ``uvicorn app.main:app`` run (2026-10-13): the access log printed the
unsubscribe token from ``POST /api/notifications/unsubscribe?token=...`` in clear, and an
unhandled error's traceback was printed twice (app.core.errors and uvicorn's "Exception in
ASGI application") without redaction, although the message lines were scrubbed.
"""

from __future__ import annotations

import io
import logging
import logging.config
from collections.abc import Iterator

import pytest
from uvicorn.config import LOGGING_CONFIG

from app.core.logging import configure_logging, install_redaction

# The shape of a real IntegrityError (a second account with the same address, produced on
# the test server; values replaced): PostgreSQL quotes the conflicting value and
# SQLAlchemy appends the statement's parameters as a Python repr.
PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2g"
UNSUBSCRIBE_TOKEN = "Zt0kenValue_9"
PII_ERROR = (
    "(psycopg.errors.UniqueViolation) duplicate key value violates unique constraint "  # noqa: S608 - error text, not a query
    '"uq_users_email"\nDETAIL:  Key (email)=(kai@example.com) already exists.\n'
    "[SQL: INSERT INTO users (id, email, password_hash) VALUES (%(id)s::UUID, "
    "%(email)s::VARCHAR, %(password_hash)s::VARCHAR)]\n"
    "[parameters: {'id': UUID('86053013-4256-48b9-9378-be15a2a679c0'), "
    f"'email': 'kai@example.com', 'password_hash': '{PASSWORD_HASH}', "
    f"'unsubscribe_token': '{UNSUBSCRIBE_TOKEN}'}}]"
)
SECRETS = ("kai@example.com", PASSWORD_HASH, "c2FsdHNhbHQ", UNSUBSCRIBE_TOKEN)


def _raise() -> None:
    raise RuntimeError(PII_ERROR)


def test_exception_text_is_redacted() -> None:
    logger = logging.getLogger("test.redaction.exceptions")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    try:
        install_redaction(logger)
        try:
            _raise()
        except RuntimeError:
            logger.exception("Job %s failed", "users.import")
        out = stream.getvalue()
    finally:
        logger.removeHandler(handler)
        logger.propagate = True
    assert "Job users.import failed" in out
    assert "Traceback (most recent call last)" in out and "RuntimeError" in out
    for secret in SECRETS:
        assert secret not in out


@pytest.fixture
def uvicorn_streams() -> Iterator[dict[str, io.StringIO]]:
    """uvicorn's own logging configuration (as ``uvicorn app.main:app`` sets it up before
    the app starts) with its two handlers writing to buffers; restored afterwards."""
    names = ("", "uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {
        name: (lg.handlers[:], lg.level, lg.propagate)
        for name, lg in ((n, logging.getLogger(n)) for n in names)
    }
    logging.config.dictConfig(LOGGING_CONFIG)
    streams = {"default": io.StringIO(), "access": io.StringIO()}
    for logger_name, key in (("uvicorn", "default"), ("uvicorn.access", "access")):
        for handler in logging.getLogger(logger_name).handlers:
            assert isinstance(handler, logging.StreamHandler)
            handler.setStream(streams[key])
    try:
        yield streams
    finally:
        for name, (handlers, level, propagate) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate


def test_uvicorn_access_log_hides_query_tokens(uvicorn_streams: dict[str, io.StringIO]) -> None:
    configure_logging()  # what the app's lifespan does once uvicorn is running
    # The call uvicorn's HTTP protocol makes for every request.
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "203.0.113.7:51776",
        "POST",
        "/api/notifications/unsubscribe?token=SECRETTOKEN123",
        "1.1",
        400,
    )
    out = uvicorn_streams["access"].getvalue()
    assert (
        '203.0.113.7:51776 - "POST /api/notifications/unsubscribe?token=[REDACTED] HTTP/1.1"' in out
    )
    assert "SECRETTOKEN123" not in out
    assert "400 Bad Request" in out


def test_uvicorn_error_traceback_is_redacted(uvicorn_streams: dict[str, io.StringIO]) -> None:
    configure_logging()
    try:
        _raise()
    except RuntimeError as exc:
        # uvicorn logs every exception that escapes the app (Starlette re-raises after
        # the app's own 500 handler has answered).
        logging.getLogger("uvicorn.error").error("Exception in ASGI application\n", exc_info=exc)
    err = uvicorn_streams["default"].getvalue()
    assert "Exception in ASGI application" in err and "RuntimeError" in err
    for secret in SECRETS:
        assert secret not in err
