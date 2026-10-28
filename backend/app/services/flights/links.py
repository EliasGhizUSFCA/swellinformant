"""Links that let the user inspect or continue booking with a third party.

These are ordinary route-search URLs (no scraping, no private endpoints): they open a new
search on Google Flights or Skyscanner for the same route and dates, so the fares shown
there may differ from the quote stored here. Every place that shows them says so
(:data:`ROUTE_SEARCH_NOTE`). A provider's own booking link, when an offer has one, is shown
first and separately (app.services.opportunity_views.offer_out).
"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

ROUTE_SEARCH_NOTE = "route search — fares may differ"


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
    """Route searches for the same trip, each labelled as such (``kind``, ``note``)."""
    return [
        {
            "label": "Google Flights",
            "url": google_flights_url(origin, destination, depart, ret, adults),
            "kind": "route_search",
            "note": ROUTE_SEARCH_NOTE,
        },
        {
            "label": "Skyscanner",
            "url": skyscanner_url(origin, destination, depart, ret, adults),
            "kind": "route_search",
            "note": ROUTE_SEARCH_NOTE,
        },
    ]
