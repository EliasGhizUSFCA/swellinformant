from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.models.enums import RankingPriority
from app.services.flights.base import NormalizedOffer, Segment, Slice, local_to_utc
from app.services.flights.filtering import (
    OfferConstraints,
    affordability_score,
    convert,
    filter_offers,
    rank_offers,
)
from app.services.flights.windows import (
    InfeasibleWindow,
    compute_travel_window,
    outbound_query_dates,
)
from app.services.matching.scoring import opportunity_scores

NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)
JBAY_TZ = "Africa/Johannesburg"


def jbay_window(**kw: object):  # type: ignore[no-untyped-def]
    zone = ZoneInfo(JBAY_TZ)
    args = dict(
        surf_start=datetime(2026, 8, 10, 7, tzinfo=zone).astimezone(UTC),
        surf_end=datetime(2026, 8, 13, 17, tzinfo=zone).astimezone(UTC),
        spot_tz=JBAY_TZ,
        destination_iata="PLZ",
        destination_tz=JBAY_TZ,
        transfer_minutes=75,
        arrival_buffer_days=2,
        departure_buffer_days=1,
        min_days_at_destination=3,
        max_trip_days=14,
        now=NOW,
    )
    args.update(kw)
    return compute_travel_window(**args)  # type: ignore[arg-type]


class TestTravelWindow:
    def test_spec_example_aug_10_13(self) -> None:
        w = jbay_window()
        assert w.recommended_arrival_date == date(2026, 8, 8)
        assert w.recommended_departure_date == date(2026, 8, 14)
        # Latest landing: 23:59 local on Aug 8 minus the 75 min transfer
        assert w.arrive_by.astimezone(ZoneInfo(JBAY_TZ)) == datetime(
            2026, 8, 8, 22, 44, tzinfo=ZoneInfo(JBAY_TZ)
        )
        assert w.depart_after.astimezone(ZoneInfo(JBAY_TZ)).date() == date(2026, 8, 14)

    def test_zero_arrival_buffer_requires_arriving_before_the_swell(self) -> None:
        w = jbay_window(arrival_buffer_days=0)
        assert w.arrive_by <= jbay_window().surf_start - timedelta(minutes=75)

    def test_min_days_extends_and_max_trip_trims(self) -> None:
        short = jbay_window(
            surf_end=datetime(2026, 8, 10, 12, tzinfo=ZoneInfo(JBAY_TZ)).astimezone(UTC),
            arrival_buffer_days=0,
            departure_buffer_days=0,
            min_days_at_destination=4,
        )
        assert (short.recommended_departure_date - short.recommended_arrival_date).days == 4
        trimmed = jbay_window(max_trip_days=4)
        assert trimmed.trimmed and trimmed.nights == 4

    def test_too_late_to_arrive(self) -> None:
        with pytest.raises(InfeasibleWindow):
            jbay_window(now=datetime(2026, 8, 8, 12, tzinfo=UTC))

    def test_query_dates_account_for_time_zones(self) -> None:
        # Honolulu is UTC-10: the departure date in Hawaii is earlier than arrival in Fiji.
        fiji = ZoneInfo("Pacific/Fiji")
        w = compute_travel_window(
            surf_start=datetime(2026, 8, 10, 7, tzinfo=fiji).astimezone(UTC),
            surf_end=datetime(2026, 8, 12, 17, tzinfo=fiji).astimezone(UTC),
            spot_tz="Pacific/Fiji",
            destination_iata="NAN",
            destination_tz="Pacific/Fiji",
            transfer_minutes=60,
            arrival_buffer_days=1,
            departure_buffer_days=1,
            min_days_at_destination=2,
            max_trip_days=10,
            now=NOW,
        )
        dates = outbound_query_dates(w, "Pacific/Honolulu", 5100, 20, max_dates=3)
        assert dates, "should find departure dates"
        # Crossing the date line west-bound: leave Honolulu on Aug 7 or 8 to land Aug 9 in Fiji.
        assert dates[0] in (date(2026, 8, 7), date(2026, 8, 8))


def seg(o: str, d: str, dep: str, o_tz: str, arr: str, d_tz: str, code: str = "XX") -> Segment:
    return Segment(
        o,
        d,
        local_to_utc(dep, o_tz),
        local_to_utc(arr, d_tz),
        dep,
        arr,
        code,
        "Test Air",
        f"{code}1",
    )


def offer(
    price: float,
    out: list[Segment],
    back: list[Segment],
    *,
    currency: str = "USD",
    expires: datetime | None = None,
    airline: str = "XX",
) -> NormalizedOffer:
    return NormalizedOffer(
        provider="test",
        provider_offer_id=f"o{price}",
        total_price=Decimal(str(price)),
        currency=currency,
        outbound=Slice(out),
        inbound=Slice(back),
        origin_timezone="America/Los_Angeles",
        destination_timezone=JBAY_TZ,
        airlines=[airline],
        airline_names=["Test Air"],
        expires_at=expires,
    )


LA, JNB = "America/Los_Angeles", JBAY_TZ
GOOD_OUT = [
    seg("SFO", "JNB", "2026-08-06T20:00:00", LA, "2026-08-07T22:30:00", JNB),
    seg("JNB", "PLZ", "2026-08-08T09:00:00", JNB, "2026-08-08T10:40:00", JNB),
]
GOOD_BACK = [
    seg("PLZ", "JNB", "2026-08-14T11:00:00", JNB, "2026-08-14T12:40:00", JNB),
    seg("JNB", "SFO", "2026-08-14T20:00:00", JNB, "2026-08-15T09:30:00", LA),
]
CONSTRAINTS = OfferConstraints(
    max_price_per_traveler=Decimal("850"),
    currency="USD",
    travelers=1,
    max_layovers=2,
    direct_only=False,
    max_flight_hours=40,
    min_days_at_destination=3,
    max_trip_days=14,
)


class TestFilters:
    def test_overnight_international_flight_accepted_on_real_arrival_time(self) -> None:
        o = offer(780, GOOD_OUT, GOOD_BACK)
        # Outbound leaves Aug 6 local in SFO and arrives Aug 8 local in PLZ.
        assert o.outbound.departure_at.astimezone(ZoneInfo(LA)).date() == date(2026, 8, 6)
        assert o.outbound.arrival_at.astimezone(ZoneInfo(JNB)).date() == date(2026, 8, 8)
        result = filter_offers([o], jbay_window(), CONSTRAINTS, now=NOW)
        assert len(result.eligible) == 1, result.rejections

    def test_budget_is_per_traveller(self) -> None:
        o = offer(1600, GOOD_OUT, GOOD_BACK)
        two = OfferConstraints(**{**CONSTRAINTS.__dict__, "travelers": 2})
        assert filter_offers([o], jbay_window(), two, now=NOW).eligible
        result = filter_offers([o], jbay_window(), CONSTRAINTS, now=NOW)
        assert not result.eligible and result.rejections["over budget"] == 1
        assert result.cheapest_rejected_per_traveler == Decimal("1600.00")

    def test_late_arrival_rejected(self) -> None:
        late = GOOD_OUT[:1] + [
            seg("JNB", "PLZ", "2026-08-08T22:00:00", JNB, "2026-08-08T23:40:00", JNB)
        ]
        roomy = OfferConstraints(**{**CONSTRAINTS.__dict__, "max_flight_hours": 60})
        result = filter_offers([offer(700, late, GOOD_BACK)], jbay_window(), roomy, now=NOW)
        assert result.rejections["arrives outside the arrival window"] == 1

    def test_return_before_swell_ends_rejected(self) -> None:
        early = [seg("PLZ", "SFO", "2026-08-13T11:00:00", JNB, "2026-08-14T05:00:00", LA)]
        result = filter_offers([offer(700, GOOD_OUT, early)], jbay_window(), CONSTRAINTS, now=NOW)
        assert result.rejections["returns outside the departure window"] == 1

    def test_layovers_direct_only_and_duration(self) -> None:
        direct = OfferConstraints(**{**CONSTRAINTS.__dict__, "direct_only": True})
        assert (
            filter_offers(
                [offer(700, GOOD_OUT, GOOD_BACK)], jbay_window(), direct, now=NOW
            ).rejections["too many layovers"]
            == 1
        )
        short = OfferConstraints(**{**CONSTRAINTS.__dict__, "max_flight_hours": 10})
        assert (
            filter_offers(
                [offer(700, GOOD_OUT, GOOD_BACK)], jbay_window(), short, now=NOW
            ).rejections["flight too long"]
            == 1
        )

    def test_expired_offer_rejected(self) -> None:
        o = offer(700, GOOD_OUT, GOOD_BACK, expires=NOW - timedelta(minutes=1))
        assert (
            filter_offers([o], jbay_window(), CONSTRAINTS, now=NOW).rejections["offer expired"] == 1
        )

    def test_currency_needs_fx_rate(self) -> None:
        o = offer(600, GOOD_OUT, GOOD_BACK, currency="EUR")
        assert not filter_offers([o], jbay_window(), CONSTRAINTS, now=NOW).eligible
        result = filter_offers([o], jbay_window(), CONSTRAINTS, now=NOW, usd_rates={"EUR": 1.1})
        assert result.eligible and result.eligible[0].total_converted == Decimal("660.00")
        assert convert(Decimal("100"), "USD", "USD", {}) == Decimal("100")

    def test_fixed_dates_check_home_departure_and_return(self) -> None:
        fixed = OfferConstraints(
            **{
                **CONSTRAINTS.__dict__,
                "date_start": date(2026, 8, 7),
                "date_end": date(2026, 8, 20),
            }
        )
        assert (
            filter_offers(
                [offer(700, GOOD_OUT, GOOD_BACK)], jbay_window(), fixed, now=NOW
            ).rejections["leaves home before your available dates"]
            == 1
        )

    def test_date_line_crossing_eastbound_arrives_before_it_departs(self) -> None:
        # Fiji (UTC+12) → Honolulu (UTC-10): arrival wall-clock date precedes departure date.
        # 22:00 Aug 15 in Fiji is 10:00 UTC; 06:30 Aug 15 in Honolulu is 16:30 UTC → 6.5 h.
        s = seg(
            "NAN",
            "HNL",
            "2026-08-15T22:00:00",
            "Pacific/Fiji",
            "2026-08-15T06:30:00",
            "Pacific/Honolulu",
        )
        assert s.arrival_at > s.departure_at
        assert s.duration_minutes == 6 * 60 + 30


class TestRanking:
    def test_priorities_change_order(self) -> None:
        cheap_slow = offer(500, GOOD_OUT, GOOD_BACK)
        fast_out = [seg("SFO", "PLZ", "2026-08-07T08:00:00", LA, "2026-08-08T09:00:00", JNB)]
        fast_back = [seg("PLZ", "SFO", "2026-08-14T10:00:00", JNB, "2026-08-14T20:00:00", LA)]
        pricey_fast = offer(840, fast_out, fast_back)
        eligible = filter_offers(
            [cheap_slow, pricey_fast], jbay_window(), CONSTRAINTS, now=NOW
        ).eligible
        assert len(eligible) == 2
        assert rank_offers(eligible, RankingPriority.CHEAPEST)[0].offer is cheap_slow
        assert rank_offers(eligible, RankingPriority.SHORTEST)[0].offer is pricey_fast

    def test_affordability(self) -> None:
        assert affordability_score(Decimal("850"), Decimal("850")) == 50
        assert affordability_score(Decimal("0"), Decimal("850")) == 100

    def test_opportunity_score_weights(self) -> None:
        comps, overall = opportunity_scores(
            "balanced", surf=92, confidence=75, affordability=85, convenience=80
        )
        assert overall == round(0.4 * 92 + 0.25 * 85 + 0.15 * 75 + 0.2 * 80)
        assert comps["surf"] == 92
        _, surf_only = opportunity_scores(
            "balanced", surf=90, confidence=60, affordability=None, convenience=None
        )
        assert surf_only == round((0.4 * 90 + 0.15 * 60) / 0.55)
        _, waves = opportunity_scores(
            "best_waves", surf=95, confidence=50, affordability=50, convenience=50
        )
        _, cheap = opportunity_scores(
            "cheapest", surf=95, confidence=50, affordability=50, convenience=50
        )
        assert waves > cheap
