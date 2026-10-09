"""Links that let the user inspect or continue booking with a third party.

These are ordinary search URLs (no scraping, no private endpoints). Prices on the
third-party site may differ from the quote stored here.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote


def google_flights_url(
    origin: str, destination: str, depart: date, ret: date, adults: int = 1
) -> str:
    q = f"Flights from {origin} to {destination} on {depart.isoformat()} through {ret.isoformat()}"
    if adults > 1:
        q += f" for {adults} adults"
    return "https://www.google.com/travel/flights?q=" + quote(q)


def skyscanner_url(origin: str, destination: str, depart: date, ret: date, adults: int = 1) -> str:
    return (
        f"https://www.skyscanner.com/transport/flights/{origin.lower()}/{destination.lower()}/"
        f"{depart:%y%m%d}/{ret:%y%m%d}/?adultsv2={adults}"
    )


def inspection_links(
    origin: str, destination: str, depart: date, ret: date, adults: int = 1
) -> list[dict[str, str]]:
    return [
        {
            "label": "Google Flights",
            "url": google_flights_url(origin, destination, depart, ret, adults),
        },
        {"label": "Skyscanner", "url": skyscanner_url(origin, destination, depart, ret, adults)},
    ]
