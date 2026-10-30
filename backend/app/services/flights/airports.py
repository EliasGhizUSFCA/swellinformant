"""Destination airports: international gateways for small regional airports.

Several target-market spots list a small regional airport as their primary one (Lakey Peak
BMU, G-Land BWX, Desert Point LOP, Bocas del Toro BOC, Puerto Escondido PXM; Pavones lists
GLF as its second airport). Real providers often return nothing for those: their regional
carriers (Wings Air, Lion Air, Air Panama, Sansa, ...) are not always sold through flight
APIs. When the regional airport returns no offers at all, flights are searched once via an
international gateway that the spot's own airport list names (data/surf_spots/spots.json:
DPS, SJO, LIR, PTY, MEX), within the search's maximum transfer time. The gateway's own,
longer transfer is then used for the arrival window, and the trip is labelled with it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

REGIONAL_AIRPORTS = frozenset({"BMU", "BWX", "LOP", "BOC", "PXM", "GLF"})
GATEWAY_AIRPORTS = frozenset({"DPS", "SJO", "LIR", "PTY", "MEX"})


class SpotAirportLike(Protocol):
    @property
    def airport_iata(self) -> str: ...

    @property
    def transfer_minutes(self) -> int: ...


def gateway_fallbacks[A: SpotAirportLike](
    spot_airports: Iterable[A], chosen_iata: str | None, max_transfer_minutes: int | None
) -> list[A]:
    """The spot's gateway airports to try when ``chosen_iata`` (a regional airport)
    returned no offers, shortest transfer first; empty for any other airport."""
    if chosen_iata not in REGIONAL_AIRPORTS:
        return []
    out = [
        a
        for a in spot_airports
        if a.airport_iata in GATEWAY_AIRPORTS
        and a.airport_iata != chosen_iata
        and (max_transfer_minutes is None or a.transfer_minutes <= max_transfer_minutes)
    ]
    return sorted(out, key=lambda a: a.transfer_minutes)
