"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from app.models.flight import FlightOffer, FlightSearch
from app.models.forecast import (
    ForecastRun,
    ForecastSource,
    SurfQualityPrediction,
    SwellEvent,
    WaveForecast,
)
from app.models.opportunity import (
    BackgroundJobLog,
    Notification,
    OpportunityMatch,
    OpportunityOffer,
)
from app.models.search import SavedSearch, SearchDestination, SearchOrigin, TravelPreference
from app.models.spot import Airport, SpotAirport, SurfSpot
from app.models.user import (
    NotificationPreference,
    User,
    UserAirport,
    UserProfile,
    UserSession,
    UserToken,
)

__all__ = [
    "Airport",
    "BackgroundJobLog",
    "FlightOffer",
    "FlightSearch",
    "ForecastRun",
    "ForecastSource",
    "Notification",
    "NotificationPreference",
    "OpportunityMatch",
    "OpportunityOffer",
    "SavedSearch",
    "SearchDestination",
    "SearchOrigin",
    "SpotAirport",
    "SurfQualityPrediction",
    "SurfSpot",
    "SwellEvent",
    "TravelPreference",
    "User",
    "UserAirport",
    "UserProfile",
    "UserSession",
    "UserToken",
    "WaveForecast",
]
