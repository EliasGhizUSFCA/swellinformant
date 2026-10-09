"""Travel window maths: swell window + buffers → acceptable flight arrival/departure instants.

Example (from the product spec): swell Aug 10–13 at the spot, arrive 2 days early, leave
1 day after → recommended arrival Aug 8 and departure Aug 14, both in the *destination's*
local calendar. Flights are then accepted when:

* the outbound flight lands between 00:00 on (arrival date − 1 day) and
  23:59 on the arrival date minus the airport→spot transfer time, and never later than
  the start of the surf window minus the transfer time;
* the return flight leaves between the later of 00:00 on the departure date and
  (end of the surf window + transfer time), and 23:59 on (departure date + 1 day).

All comparisons are between real UTC instants computed from each itinerary's actual
local timestamps and airport time zones, so overnight flights, DST and the international
date line are handled correctly. Departure dates to query at the origin are derived from
the arrival deadline minus the plausible flight-time range, converted to the origin's
local calendar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.services.geo import haversine_km

ARRIVAL_FLEX_DAYS = 1
DEPARTURE_FLEX_DAYS = 1
MIN_BOOKING_LEAD = timedelta(hours=24)


@dataclass(frozen=True)
class TravelWindow:
    destination_iata: str
    destination_tz: str
    spot_tz: str
    transfer_minutes: int
    surf_start: datetime
    surf_end: datetime
    recommended_arrival_date: date
    recommended_departure_date: date
    arrive_after: datetime
    arrive_by: datetime
    depart_after: datetime
    depart_before: datetime
    trimmed: bool = False

    @property
    def nights(self) -> int:
        return (self.recommended_departure_date - self.recommended_arrival_date).days


class InfeasibleWindow(ValueError):
    """The surf window cannot be reached with the requested buffers."""


def _start_of_day(d: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, time(0, 0), tzinfo=tz).astimezone(UTC)


def _end_of_day(d: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, time(23, 59), tzinfo=tz).astimezone(UTC)


def compute_travel_window(
    *,
    surf_start: datetime,
    surf_end: datetime,
    spot_tz: str,
    destination_iata: str,
    destination_tz: str,
    transfer_minutes: int,
    arrival_buffer_days: int,
    departure_buffer_days: int,
    min_days_at_destination: int,
    max_trip_days: int,
    now: datetime,
) -> TravelWindow:
    spot_zone = ZoneInfo(spot_tz)
    transfer = timedelta(minutes=transfer_minutes)
    first_surf_day = surf_start.astimezone(spot_zone).date()
    last_surf_day = (surf_end - timedelta(minutes=1)).astimezone(spot_zone).date()

    arrival = first_surf_day - timedelta(days=arrival_buffer_days)
    departure = last_surf_day + timedelta(days=departure_buffer_days)
    trimmed = False
    if (departure - arrival).days < min_days_at_destination:
        departure = arrival + timedelta(days=min_days_at_destination)
    if (departure - arrival).days > max_trip_days:
        departure = arrival + timedelta(days=max_trip_days)
        trimmed = True

    arrive_by = min(_end_of_day(arrival, spot_zone), surf_start) - transfer
    arrive_after = _start_of_day(arrival - timedelta(days=ARRIVAL_FLEX_DAYS), spot_zone)
    effective_surf_end = min(surf_end, _start_of_day(departure, spot_zone)) if trimmed else surf_end
    depart_after = max(_start_of_day(departure, spot_zone), effective_surf_end + transfer)
    depart_before = _end_of_day(departure + timedelta(days=DEPARTURE_FLEX_DAYS), spot_zone)

    if arrive_by < now + MIN_BOOKING_LEAD:
        raise InfeasibleWindow(f"too late to arrive {arrival_buffer_days} day(s) before the swell")
    arrive_after = max(arrive_after, now + MIN_BOOKING_LEAD)
    if depart_after >= depart_before or arrive_after >= arrive_by:
        raise InfeasibleWindow("no feasible arrival/departure window")
    return TravelWindow(
        destination_iata=destination_iata,
        destination_tz=destination_tz,
        spot_tz=spot_tz,
        transfer_minutes=transfer_minutes,
        surf_start=surf_start,
        surf_end=surf_end,
        recommended_arrival_date=arrival,
        recommended_departure_date=departure,
        arrive_after=arrive_after,
        arrive_by=arrive_by,
        depart_after=depart_after,
        depart_before=depart_before,
        trimmed=trimmed,
    )


def estimated_flight_hours(km: float) -> float:
    """Rough door-to-door air time used only to choose which dates to query."""
    if km < 3000:
        return km / 800 + 1.0
    return km / 820 + 3.5  # long-haul usually means at least one connection


def outbound_query_dates(
    window: TravelWindow,
    origin_tz: str,
    distance_km: float,
    max_flight_hours: float,
    max_dates: int,
) -> list[date]:
    """Origin-local departure dates that can produce an arrival inside the window.

    Ordered by preference: the date that lands closest to the recommended arrival first.
    """
    zone = ZoneInfo(origin_tz)
    min_hours = max(0.75, distance_km / 950)
    earliest_departure = window.arrive_after - timedelta(hours=max_flight_hours)
    latest_departure = window.arrive_by - timedelta(hours=min_hours)
    if latest_departure <= earliest_departure:
        return []
    preferred = (
        (window.arrive_by - timedelta(hours=estimated_flight_hours(distance_km) + 2))
        .astimezone(zone)
        .date()
    )
    first = earliest_departure.astimezone(zone).date()
    last = latest_departure.astimezone(zone).date()
    candidates = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    candidates.sort(key=lambda d: (abs((d - preferred).days), -d.toordinal()))
    return candidates[:max_dates]


def great_circle_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    return haversine_km(a[0], a[1], b[0], b[1])
