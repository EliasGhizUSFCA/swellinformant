"""Refused credentials of the flight and email providers, recorded for the admin alerts
(spec 16: alert an admin when a provider's credentials expire; review r1-spec-SPEC-7).

The forecast providers' refused keys are recorded by their own path (the newest attempt of
each source, and the provider manager's recorded health). The flight provider (Duffel,
Amadeus) and the email provider (Resend, SendGrid) answer HTTP 401/403 for an expired or
revoked key; the search or message then fails permanently and nothing else would tell an
admin. Here such a refusal is recorded in Redis under ``service-credentials:v1:<service>``
(``service`` is ``flights_<provider>`` or ``email_<provider>``; a preview deployment keeps
its own keys, ``app.core.redis.deployment_scope``) and the next accepted request clears it.
``app.services.health`` turns a record into the admin problem
``provider_credentials:<service>`` (failing), which ``ops_alerts`` emails to the admins, and
whose recovery notice follows the clearing.

Best effort: a Redis error is logged and swallowed, so a provider call never fails because
of the record. A recorded success costs one Redis command (DEL).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import redis

from app.core.database import utcnow
from app.core.logging import redact
from app.core.redis import deployment_scope, get_redis

logger = logging.getLogger(__name__)

KEY = "service-credentials:v1:{service}"
TTL_SECONDS = 30 * 86400
DETAIL_MAX_CHARS = 300


def service_name(kind: str, provider: str) -> str:
    """``flights_duffel``, ``email_resend``, ...: the service a record is kept under."""
    return f"{kind}_{provider}".lower()


def key(service: str) -> str:
    return KEY.format(service=f"{deployment_scope()[1:]}:{service}".lstrip(":"))


def record_refused(kind: str, provider: str, detail: str, now: datetime | None = None) -> None:
    """The provider refused the credentials (HTTP 401/403): record it (redacted)."""
    service = service_name(kind, provider)
    payload = {
        "service": service,
        "kind": kind,
        "provider": provider,
        "at": (now or utcnow()).astimezone(UTC).isoformat(),
        "detail": redact(detail)[:DETAIL_MAX_CHARS],
    }
    try:
        get_redis().set(key(service), json.dumps(payload), ex=TTL_SECONDS)
    except redis.RedisError as exc:
        logger.warning("Refused %s credentials not recorded: %s", service, type(exc).__name__)
        return
    logger.warning("%s refused the credentials: %s", service, payload["detail"])


def record_accepted(kind: str, provider: str) -> None:
    """The provider accepted a request: any recorded refusal is over."""
    service = service_name(kind, provider)
    try:
        get_redis().delete(key(service))
    except redis.RedisError as exc:
        logger.warning("Credentials record of %s not cleared: %s", service, type(exc).__name__)


def refused(services: Iterable[str]) -> list[dict[str, Any]]:
    """The recorded refusals of these services (one MGET; empty when Redis is down)."""
    names = sorted(set(services))
    if not names:
        return []
    try:
        values = get_redis().mget([key(s) for s in names])
    except redis.RedisError as exc:
        logger.warning("Service credentials not read: %s", type(exc).__name__)
        return []
    out: list[dict[str, Any]] = []
    for raw in values:
        if not raw:
            continue
        try:
            data = json.loads(str(raw))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("service"):
            out.append(data)
    return out
