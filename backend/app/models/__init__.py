"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from app.models.flight import FlightOffer, FlightSearch
from app.models.forecast import (
    ForecastRun,
    ForecastSource,
    SurfQualityPrediction,
    SwellEvent,
    WaveForecast,
)
from app.models.forecast_engine import (
    ForecastComparison,
    ForecastEnsembleStat,
    MapField,
    PredictionArchive,
    SpotPhysicalProfile,
)
from app.models.messaging import EmailMessage
from app.models.ml import (
    ForecastAccuracyMetric,
    MLExperiment,
    MLModelVersion,
    PredictionValidation,
)
from app.models.observations import (
    BuoyObservation,
    BuoyStation,
    NearshoreObservation,
    PointForecast,
)
from app.models.opportunity import (
    BackgroundJobLog,
    Notification,
    OpportunityMatch,
    OpportunityOffer,
)
from app.models.research import (
    SpotResearchAssessment,
    SpotResearchClaim,
    SpotResearchSource,
)
from app.models.search import SavedSearch, SearchDestination, SearchOrigin, TravelPreference
from app.models.spot import Airport, SpotAirport, SurfSpot
from app.models.sync import SyncRun
from app.models.tides import SpotTideStation, TidePrediction, TideStation
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
    "BuoyObservation",
    "BuoyStation",
    "EmailMessage",
    "FlightOffer",
    "FlightSearch",
    "ForecastAccuracyMetric",
    "ForecastComparison",
    "ForecastEnsembleStat",
    "ForecastRun",
    "ForecastSource",
    "MLExperiment",
    "MLModelVersion",
    "MapField",
    "NearshoreObservation",
    "Notification",
    "NotificationPreference",
    "OpportunityMatch",
    "OpportunityOffer",
    "PointForecast",
    "PredictionArchive",
    "PredictionValidation",
    "SavedSearch",
    "SearchDestination",
    "SearchOrigin",
    "SpotAirport",
    "SpotPhysicalProfile",
    "SpotResearchAssessment",
    "SpotResearchClaim",
    "SpotResearchSource",
    "SpotTideStation",
    "SurfQualityPrediction",
    "SurfSpot",
    "SwellEvent",
    "SyncRun",
    "TidePrediction",
    "TideStation",
    "TravelPreference",
    "User",
    "UserAirport",
    "UserProfile",
    "UserSession",
    "UserToken",
    "WaveForecast",
]
