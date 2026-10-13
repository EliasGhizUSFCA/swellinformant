"""Logging configuration with a redaction filter for secrets and personal data."""

from __future__ import annotations

import logging
import re
import sys

from app.core.config import get_settings

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(authorization:\s*)(bearer|basic)\s+[^\s'\"]+"), r"\1\2 [REDACTED]"),
    (
        # key=value, JSON ("key": "value") and Python reprs ('key': 'value', as in the
        # "[parameters: ...]" of a SQLAlchemy error); a quoted value is removed whole.
        re.compile(
            r"(?i)([\"']?(password_hash|password|token|secret|api_key|apikey|access_token)"
            r"[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,'\"}]+)"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(?i)([?&](key|token|apikey|api_key)=)[^&\s]+"), r"\1[REDACTED]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"\+\d{8,15}"), "[PHONE]"),
]


def redact(message: str) -> str:
    for pattern, repl in _REDACTIONS:
        message = pattern.sub(repl, message)
    return message


class RedactingFilter(logging.Filter):
    """Scrubs tokens, passwords, emails and phone numbers from every log record.

    The text of a logged exception is scrubbed too: tracebacks quote error messages, and
    those can hold an address, a token or a password hash (PostgreSQL's "Key (email)=(...)
    already exists", SQLAlchemy's "[parameters: ...]", SMTP recipient errors).

    ``keep_args``: redact each string argument in place instead of merging the arguments
    into the message, for formatters that read ``record.args`` themselves (uvicorn's access
    log formatter unpacks them into the client address, request line and status).
    """

    def __init__(self, keep_args: bool = False) -> None:
        super().__init__()
        self.keep_args = keep_args

    def filter(self, record: logging.LogRecord) -> bool:
        if self.keep_args and isinstance(record.args, tuple):
            record.msg = redact(str(record.msg))
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        else:
            try:
                msg = record.getMessage()
            except Exception:  # pragma: no cover - malformed record
                return True
            record.msg = redact(msg)
            record.args = ()
        if record.exc_info and not record.exc_text:
            # Formatters reuse a record's exc_text instead of formatting the exception again.
            record.exc_text = _EXCEPTION_FORMATTER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


_EXCEPTION_FORMATTER = logging.Formatter()


def install_redaction(logger: logging.Logger, keep_args: bool = False) -> None:
    """Attach the redacting filter to every handler of ``logger`` (idempotent)."""
    for handler in logger.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter(keep_args=keep_args))


def configure_logging(add_handler: bool = True) -> None:
    """Configure the root logger.

    Celery installs its own root handlers (and redirects stdout into logging), so worker and
    beat processes call this with ``add_handler=False``: the redaction filter is attached to
    Celery's handlers instead of adding a second stdout handler that would duplicate lines.
    """
    settings = get_settings()
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    if add_handler and not any(getattr(h, "_swell", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        handler._swell = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    install_redaction(root)
    # Under uvicorn: its error log (which prints the traceback of every exception that
    # escapes the app) and its access log (request lines with their query strings, e.g. the
    # unsubscribe token) have their own handlers. No-ops when uvicorn is not running.
    install_redaction(logging.getLogger("uvicorn"))
    install_redaction(logging.getLogger("uvicorn.access"), keep_args=True)
    # httpx logs full request URLs (which can contain API keys) at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
