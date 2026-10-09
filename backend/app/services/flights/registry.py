"""Build the configured flight provider with database-backed airport lookups."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import Airport
from app.services.flights.amadeus import AmadeusProvider
from app.services.flights.base import FlightProvider
from app.services.flights.demo import AirportInfo, DemoFlightProvider
from app.services.flights.duffel import DuffelProvider


class AirportDirectory:
    """In-memory snapshot of the airports table (small, read-mostly reference data)."""

    def __init__(self, db: Session) -> None:
        self._rows = {
            a.iata: AirportInfo(a.iata, a.latitude, a.longitude, a.timezone)
            for a in db.scalars(select(Airport)).all()
        }

    def get(self, code: str) -> AirportInfo | None:
        return self._rows.get(code.upper())

    def tz(self, code: str) -> str | None:
        info = self.get(code)
        return info.timezone if info else None

    def all(self) -> list[AirportInfo]:
        return list(self._rows.values())


def build_flight_provider(db: Session, settings: Settings | None = None) -> FlightProvider:
    settings = settings or get_settings()
    directory = AirportDirectory(db)
    if settings.flight_provider == "duffel":
        return DuffelProvider(directory.tz, settings)
    if settings.flight_provider == "amadeus":
        return AmadeusProvider(directory.tz, settings)
    return DemoFlightProvider(directory.get)


def provider_label(name: str) -> str:
    return {"duffel": "Duffel", "amadeus": "Amadeus Enterprise", "demo": "DEMO (mock fares)"}.get(
        name, name
    )
