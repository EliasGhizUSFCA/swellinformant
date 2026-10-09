"""Logging configuration with a redaction filter for secrets and personal data."""

from __future__ import annotations

import logging
import re
import sys

from app.core.config import get_settings

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(authorization:\s*)(bearer|basic)\s+[^\s'\"]+"), r"\1\2 [REDACTED]"),
    (
        re.compile(
            r"(?i)(\"?(password|token|secret|api_key|apikey|access_token)\"?\s*[:=]\s*)\"?[^\s,'\"}]+"
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
    """Scrubs tokens, passwords, emails and phone numbers from every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed record
            return True
        record.msg = redact(msg)
        record.args = ()
        return True


def install_redaction(logger: logging.Logger) -> None:
    """Attach the redacting filter to every handler of ``logger`` (idempotent)."""
    for handler in logger.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())


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
    # httpx logs full request URLs (which can contain API keys) at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
