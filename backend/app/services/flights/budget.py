"""Per-user daily budget of provider flight searches (FLIGHT_USER_DAILY_QUOTA).

One Redis counter per user and UTC day, the same pattern as the provider quota
(app.core.redis.consume_quota): a search is counted before the provider is called, and a
refused search is never counted. Only provider calls count: a shared cache hit
(app.services.flights.service.get_or_search) is free, so many users chasing the same swell
share one search without spending each other's budget. Offer re-validation right before an
alert is not a search and is not counted (it is bounded by the alert rules).

Redis being unavailable never blocks a search (the provider's own daily quota,
FLIGHT_DAILY_QUOTA, still applies): the failure is logged and the search proceeds.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import redis

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

KEY_PREFIX = "quota:flights-user"
KEY_TTL_SECONDS = 60 * 60 * 48


class UserFlightBudgetExceeded(Exception):
    """The user's provider flight searches for today (UTC) are used up."""

    def __init__(self, user_id: object, limit: int) -> None:
        self.user_id = user_id
        self.limit = limit
        super().__init__(f"daily flight-search limit of {limit} reached for this account")


def budget_key(user_id: object, now: datetime) -> str:
    return f"{KEY_PREFIX}:{user_id}:{now.astimezone(UTC):%Y%m%d}"


def consume_user_search(user_id: object, limit: int, now: datetime) -> int | None:
    """Count one provider search for ``user_id`` today; raises UserFlightBudgetExceeded
    when the user already used ``limit`` today. Returns the new count (None when Redis is
    unavailable and the search is let through)."""
    key = budget_key(user_id, now)
    try:
        client = get_redis()
        used = int(client.incr(key))
        if used == 1:
            client.expire(key, KEY_TTL_SECONDS)
        if used > limit:
            client.decr(key)
            raise UserFlightBudgetExceeded(user_id, limit)
        return used
    except redis.RedisError as exc:
        logger.warning("Per-user flight budget unavailable (%s); continuing", type(exc).__name__)
        return None


def searches_used(user_id: object, now: datetime) -> int:
    """Provider searches counted for ``user_id`` today (0 when unknown)."""
    try:
        value = get_redis().get(budget_key(user_id, now))
    except redis.RedisError:
        return 0
    return int(value or 0)


def searches_left(user_id: object, limit: int, now: datetime) -> int:
    return max(0, limit - searches_used(user_id, now))
