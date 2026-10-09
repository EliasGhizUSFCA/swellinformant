# Matching, travel windows, flights and alerts

Source: `backend/app/services/matching/`, `flights/` and `notifications/`.

## 1. Saved search → candidate events (job D, `matching.match`)

For every **active** saved search and every active swell event at a spot the search
allows (all spots, chosen spots, or regions/countries; optional break-type filter):

1. **Lead time.** A *new* match is created only when the event starts
   `DETECTION_LEAD_MIN_DAYS` to `DETECTION_LEAD_MAX_DAYS` ahead (default 5–10 days; about
   7 days is typical). Existing matches keep updating until the swell passes, so a match
   found at day 8 is still tracked at day 3.
2. **User window.** The event's daylight timesteps are re-filtered with the search's own
   criteria:
   * minimum quality label
   * breaking-height range (mid of the estimate)
   * minimum period
   * maximum wind speed
   * wind requirement: any / not onshore / offshore-ish (glassy, offshore,
     cross-offshore)
   * optional swell-direction window
   * minimum consistency

   The qualifying hours are then clustered with the same rules as event detection. The
   best cluster (highest sum of scores) of at least `min_window_hours` becomes the
   match's surf window. An event detected at "Fair" can therefore give an "Excellent"
   user window of 6 hours, or no match at all.
3. **Dates.**
   * *Flexible* searches accept any window within `horizon_days`.
   * *Fixed-date* searches require the surf window's local dates to fall inside the
     user's available dates. Flight dates are checked again in step 5.
4. **Airport.** The spot's primary airport is preferred, then the shortest transfer,
   within the search's `max_transfer_minutes` when that is set. No usable airport gives
   a *surf-only* match.
5. Matches are unique per (search, event). Re-running never duplicates them.
   * When an event stops qualifying (downgraded forecast, edited search, swell passed),
     its matches become `expired` with a reason.
   * When detection merges events, matches move to the surviving event, so alert
     history and de-duplication carry over.

## 2. Travel window (`flights/windows.py`)

Example from the spec: swell Aug 10–13, arrive 2 days early, leave 1 day after →
recommended **arrival Aug 8** and **departure Aug 14**, in the *destination's* calendar.

```
arrival_date   = first local surf day − arrival_buffer_days      (default 2)
departure_date = last local surf day  + departure_buffer_days    (default 1)
  then stretched to min_days_at_destination / trimmed to max_trip_days

arrive_after  = 00:00 on (arrival_date − 1)             in spot time zone
arrive_by     = min(23:59 on arrival_date, surf start) − airport→spot transfer
depart_after  = max(00:00 on departure_date, surf end + transfer)
depart_before = 23:59 on (departure_date + 1)
```

* Every bound is converted to a UTC instant from local time + IANA zone. Each itinerary's
  real local timestamps and airport time zones are compared on the same basis. Overnight
  flights, DST changes and the date line therefore behave correctly; unit tests cover
  Pacific crossings.
* A window that would need you to arrive within 24 h raises `InfeasibleWindow`. The match
  then becomes surf-only with the reason ("too late to arrive 2 day(s) before the swell").
* Outbound query dates are chosen in the **origin's** calendar from the arrival deadline
  minus a distance-based flight-time estimate. Up to 2 dates per origin; the closest to
  the recommended arrival comes first.

## 3. Flight discovery (job E, `flights.discover`)

Flights are searched **only** for surf-qualified matches, never speculatively.

* **Origins.** The search's home airports, plus nearby airports within
  `max_origin_ground_km` when the user opted in. Capped per search.
* **Query plan.** The first-choice date for every origin, then second choices. Capped by
  `FLIGHT_MAX_QUERIES_PER_MATCH` (3).
* **Shared cache.** `flight_searches.cache_key` = provider + route + dates + passengers +
  cabin + currency + max connections. Results live for `FLIGHT_CACHE_TTL_HOURS` (6), so
  many users chasing the same swell share one provider call.
* **Quota.** `FLIGHT_DAILY_QUOTA` provider calls per day (Redis counter). When it runs
  out, the job stops early and reports `quota_exhausted`. The match keeps its last
  offers and is retried later.
* **Refresh.** A match is re-searched after `FLIGHT_REFRESH_HOURS` (12), immediately when
  its recommended travel dates or destination airport change, or when the user presses
  "Refresh flights" (rate-limited to 6 per hour).

### Filters (each rejected offer counts toward a reason shown in the UI)

| Rejection reason | Rule |
|------------------|------|
| offer expired | provider `expires_at` has passed |
| no FX rate | quoted currency ≠ budget currency and `FX_RATES_USD_JSON` lacks a rate (offers are never guessed into budget) |
| over budget | total price ÷ travellers > max price per traveller |
| too many layovers | stops on either leg > `max_layovers` (0 when direct only) |
| flight too long | either leg longer than `max_flight_hours` |
| arrives outside the arrival window | outbound arrival not in `[arrive_after, arrive_by]` |
| returns outside the departure window | return departure not in `[depart_after, depart_before]` |
| leaves home before / returns after your available dates | fixed-date searches, origin-local dates |
| too few days at destination / trip too long | destination-local nights |

When everything is over budget, the cheapest rejected fare is stored and shown ("cheapest
fare found was $1,240"). The UI can then explain *why* nothing matched.

### Offer scores and ranking

* `affordability = 100 − 50 × price/budget` (100 = free, 50 = exactly at budget)
* `convenience = 100 − 40 × longest_leg/max_flight_hours − 8 × stops − min(20, transfer_min/30)`
* Offer order per search priority (+5 for a preferred airline):
  * cheapest: 0.85 affordability + 0.15 convenience
  * shortest: 0.85 convenience + 0.15 affordability
  * otherwise: 0.55 affordability + 0.45 convenience
* The best offer and up to 5 alternatives are linked to the match.

### Opportunity score (0–100)

`surf = 0.7 × peak score + 0.3 × average window score`, combined with confidence,
affordability and convenience. Weights per priority (override with `RANKING_WEIGHTS_JSON`):

| Priority | Surf | Affordability | Confidence | Convenience |
|----------|-----:|--------------:|-----------:|------------:|
| balanced | 0.40 | 0.25 | 0.15 | 0.20 |
| best_waves | 0.60 | 0.15 | 0.15 | 0.10 |
| cheapest | 0.25 | 0.50 | 0.10 | 0.15 |
| shortest | 0.25 | 0.15 | 0.10 | 0.50 |

Surf-only matches have no flight components; the weights renormalise over the rest.

### Booking links

Offers link to the provider's own booking flow when it returns one. Otherwise they link
to an ordinary Google Flights / Skyscanner search URL for the same route and dates (no
scraping, no private endpoints). Fares there may differ.

## 4. Alerts (job F, `alerts.generate`)

Before alerting, the best offer is **re-validated** with the provider when it supports
it (Duffel: `GET /air/offers/{id}`):

* unavailable → the cached search is expired and the match is re-searched;
* price changed → the new price is stored.

Alerts are not generated while forecast data is stale.

| Kind | When |
|------|------|
| `new_opportunity` | first alert with a fare, or flights appeared for a surf-only match |
| `surf_only` | first alert with no qualifying fare, if the search opted into surf-only alerts |
| `improved` | peak label went up, or peak score rose ≥ `ALERT_SCORE_IMPROVEMENT` (10) |
| `price_drop` | same currency, drop ≥ `ALERT_PRICE_DROP_PCT` (10 %) **and** ≥ `ALERT_PRICE_DROP_MIN_AMOUNT` (50) |
| `schedule_change` | recommended arrival or departure moved by ≥ 2 days |

Follow-ups (everything after the first alert) only go out when the search has "notify on
updates" enabled.

**De-duplication.** `notifications.dedup_key` is unique. It hashes
(user, swell event, channel, kind, fingerprint), where the fingerprint covers quality
label, price bucket, travel dates and origin. Consequences:

* re-running the job, or two workers racing, cannot queue the same alert twice;
* two overlapping saved searches of one user that find the same swell produce one alert,
  not two.

`MAX_ALERTS_PER_USER_PER_DAY` caps the volume. Pausing or deleting a search marks its
queued notifications `skipped`. Delivery also re-checks at send time that the
opportunity still exists and its search is active.

## 5. Delivery (job G, `notifications.deliver`)

* Queued rows are claimed with `SELECT … FOR UPDATE SKIP LOCKED` (concurrent workers never
  double-send).
* Rows stuck in `sending` after `NOTIFICATION_SENDING_TIMEOUT_MINUTES` are reclaimed.
* Preferences, verified email / phone and opt-outs are re-checked at send time.
* Transient provider errors retry with exponential backoff up to
  `NOTIFICATION_MAX_ATTEMPTS`. Permanent errors (invalid number, unsubscribed) fail
  immediately with the reason stored.
* Email carries a one-click unsubscribe link (`List-Unsubscribe` header + footer link).
* SMS honours Twilio STOP via the status/inbound webhook (`/api/webhooks/twilio`,
  signature-verified).
