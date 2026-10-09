"""String enums shared by models, schemas and services.

They are stored as VARCHAR columns with CHECK constraints (non-native enums), which keeps
Alembic migrations simple when values are added later.
"""

from __future__ import annotations

from enum import StrEnum


class BreakType(StrEnum):
    REEF = "reef"
    POINT = "point"
    BEACH = "beach"
    RIVERMOUTH = "rivermouth"
    SLAB = "slab"


class SkillLevel(StrEnum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"
    EXPERT = "expert"


class TidePreference(StrEnum):
    LOW = "low"
    LOW_TO_MID = "low_to_mid"
    MID = "mid"
    MID_TO_HIGH = "mid_to_high"
    HIGH = "high"
    ALL = "all"


class TokenPurpose(StrEnum):
    EMAIL_VERIFY = "email_verify"
    PASSWORD_RESET = "password_reset"  # noqa: S105 (token purpose, not a secret)


class ForecastRunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class QualityLabel(StrEnum):
    POOR = "poor"
    FAIR = "fair"
    GOOD = "good"
    VERY_GOOD = "very_good"
    EXCELLENT = "excellent"
    EXCEPTIONAL = "exceptional"


class ConfidenceLabel(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class SwellEventStatus(StrEnum):
    ACTIVE = "active"  # forecast, upcoming or in progress
    DOWNGRADED = "downgraded"  # latest forecast no longer shows a qualifying window
    CANCELLED = "cancelled"  # merged into another event
    PASSED = "passed"  # end time is in the past


class SearchStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"


class DateMode(StrEnum):
    FIXED = "fixed"
    FLEXIBLE = "flexible"


class DestinationMode(StrEnum):
    ALL = "all"
    REGIONS = "regions"
    SPOTS = "spots"


class NotificationChannelPref(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    BOTH = "both"
    DASHBOARD = "dashboard"


class RankingPriority(StrEnum):
    BALANCED = "balanced"
    BEST_WAVES = "best_waves"
    CHEAPEST = "cheapest"
    SHORTEST = "shortest"


class WindRequirement(StrEnum):
    ANY = "any"
    NOT_ONSHORE = "not_onshore"
    OFFSHORE = "offshore"


class CabinClass(StrEnum):
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"


class FlightSearchStatus(StrEnum):
    SUCCESS = "success"
    NO_RESULTS = "no_results"
    FAILED = "failed"


class OfferValidation(StrEnum):
    UNVALIDATED = "unvalidated"
    VALID = "valid"
    PRICE_CHANGED = "price_changed"
    UNAVAILABLE = "unavailable"


class MatchStatus(StrEnum):
    PENDING_FLIGHTS = "pending_flights"  # surf matches, flights not searched yet
    FLIGHT_FOUND = "flight_found"  # budget-qualified offer exists
    SURF_ONLY = "surf_only"  # surf matches but no eligible flight was found
    EXPIRED = "expired"  # event passed, was downgraded, or no longer matches
    DISMISSED = "dismissed"  # user dismissed it


class NotificationChannel(StrEnum):
    EMAIL = "email"
    SMS = "sms"


class NotificationKind(StrEnum):
    NEW_OPPORTUNITY = "new_opportunity"
    IMPROVED = "improved"
    PRICE_DROP = "price_drop"
    SCHEDULE_CHANGE = "schedule_change"
    SURF_ONLY = "surf_only"


class NotificationStatus(StrEnum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


QUALITY_ORDER: list[QualityLabel] = [
    QualityLabel.POOR,
    QualityLabel.FAIR,
    QualityLabel.GOOD,
    QualityLabel.VERY_GOOD,
    QualityLabel.EXCELLENT,
    QualityLabel.EXCEPTIONAL,
]
