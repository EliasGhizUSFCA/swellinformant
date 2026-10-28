# Matching, travel windows, flights and alerts

Source: `backend/app/services/matching/`, `flights/` and `notifications/`. This is system B
of the Swell 2.0 design (spec sections 2, 26-29; design decision D15): it reads the
published forecasts, finds the swells that fit each saved search, searches flights only when
that is justified, and alerts users when surf and flights line up.

## 0. When things run

| What | When | Where |
|------|------|-------|
| Matching (`matching.match`) | after every forecast publication (the sync's `match` stage), when a search is saved or resumed (a kick), once a day at 03:45 UTC as housekeeping | `services/sync/orchestrator.py` `AFTER_PUBLISH`, `api/searches.py`, `jobs/schedule.py` `MATCHING_HOUSEKEEPING` |
| Flight discovery (`flights.discover`) | after every publication (the `flights` stage, right after matching), on "Re-check flights", after the Celery housekeeping chain | never on a timer |
| Alerts (`alerts.generate`) | after every publication, every 15 min (held alerts: email verified later, daily cap reset) | `jobs/schedule.py` |
| Delivery (`notifications.deliver`) | after alerts, every 2 min (floored to `JOB_INTERVAL_FLOOR_MINUTES` on hosted deployments) | `jobs/schedule.py` |

The sync runs `detect → match → flights → alerts → deliver` after each publication, so a
new model run reaches matching once, without a second timer duplicating it. The daily
housekeeping run expires matches whose window has passed even when no new forecast
arrives. Under Celery a housekeeping run also chains flights and alerts (they only act on
new or changed matches); the sync daemon runs it on its own and leaves the rest to the next
publication.

**The periodic flight search is gone.** Swell 2.0 removed the hourly `flight-discovery`
timer and the `FLIGHT_REFRESH_HOURS` (12 h) re-search of every match. `FLIGHT_REFRESH_HOURS`
is no longer a setting: Settings ignore unknown keys (`extra="ignore"`), so an old env file
that still sets it keeps loading.

## 1. Saved search → candidate events (`matching.match`)

For every **active** saved search and every active swell event at a spot the search
allows (all spots, chosen spots, or regions/countries; optional break-type filter):

1. **Lead time (spec 28).** A *new* match is created only when the event starts between the
   search's own `alert_lead_min_days` and `alert_lead_max_days` from now (whole days;
   defaults `ALERT_LEAD_DEFAULT_MIN_DAYS` / `_MAX_DAYS` = 2 / 10; the wizard's "Alert lead
   time"). They apply inside the global detection window `DETECTION_LEAD_MIN_DAYS` /
   `_MAX_DAYS` (1 / 10 days): a search asking for 0 days still waits for the 1-day floor
   (a trip needs at least 24 h to book, `MIN_BOOKING_LEAD`), and 16 days is capped at the
   10-day forecast horizon. Existing matches keep updating until the swell passes, so a
   match found at day 8 is still tracked at day 3. Before Swell 2.0 a global 5-day floor hid
   every swell first forecast 3-5 days ahead (the audit's probe: 3.5 and 4.0 days → no
   match); `tests/integration/test_flight_targeting.py` now matches one 4 days out.
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
   a *surf-only* match. A match whose flights were found via a gateway airport (section 3)
   keeps the gateway while its trip is unchanged.
5. Matches are unique per (search, event). Re-running never duplicates them.
   * When an event stops qualifying (downgraded forecast, edited search, swell passed),
     its matches become `expired` with a reason.
   * When detection merges events, matches move to the surviving event, so alert
     history and de-duplication carry over.

### What sends a match to the flight search (and what does not)

A match is set to `pending_flights` (searched by the next flight run) only when:

| Trigger | Example |
|---------|---------|
| new match | a swell starts to fit the search inside its lead time |
| material change | the recommended arrival or departure date moved (surf window shifted, buffers edited) or the destination airport changed |
| quality class crossing the threshold | an `expired` match (the forecast fell below the search's quality, size or wind criteria) qualifies again |
| explicit request | "Re-check flights" on the opportunity (`flights_requested_at`) |
| fare re-check before an alert | the best fare expired, sold out or rose above the budget when an alert was about to be sent (section 4) |

Nothing else re-queries flights: an unchanged forecast never searches again, whatever the
age of the quote (`test_an_unchanged_forecast_never_searches_or_reprices_flights_again`
replays the audit's probe — 30-minute quotes, three hourly cycles — and counts zero provider
calls; before Swell 2.0 each cycle searched once).

## 2. Travel window (`flights/windows.py`)

Example from the spec: swell Nov 10–13, arrive 2 days early, leave 1 day after →
recommended **arrival Nov 8** and **departure Nov 14**, in the *destination's* calendar.

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

## 3. Flight discovery (`flights.discover`)

Flights are searched **only** for surf-qualified matches in `pending_flights` or with an
explicit request, never speculatively.

* **Origins.** The search's home airports (up to 5), plus nearby airports within
  `max_origin_ground_km` when the user opted in, at most 5 in all (`MAX_ORIGINS`).
* **Per-match budget.** The first-choice date of **every** origin is searched, then
  second-choice dates up to `FLIGHT_MAX_QUERIES_PER_MATCH` (3) queries in all: a 5-origin
  search makes 5 queries (first choices), a 1-origin search 2 (both dates)
  (`per_match_budget`).
* **Layovers.** The provider is asked for the search's `max_layovers` (0-3) connections per
  direction (0 for "direct only"); a choice of 3 is no longer queried as 2.
* **Shared cache, deduplicated across users.** `flight_searches.cache_key` = provider +
  route + dates + passengers + cabin + currency + max connections. Every user and search
  with the same trip reads the same cached search, so a new swell needing one route costs
  one provider call however many users chase it
  (`test_a_new_match_searches_each_unique_route_once_across_users`).
* **Cache lifetime.** A search is served from the cache until the **earlier** of
  `FLIGHT_CACHE_TTL_HOURS` (6) and the expiry of its first offer (`cache_expiry`; Duffel
  quotes last about 30 minutes, the demo provider's 24 h). A cache hit therefore never holds
  only expired quotes, and an explicit "Re-check flights" after an expiry asks the provider
  again. A failed search (unsupported route) is remembered for 3 h.
* **Expired quotes are never shown as bookable.** The opportunity page marks them "Quote
  expired — re-check flights" (`OfferOut.expired`, `MatchSummary.best_offer_expired`) and
  drops the provider's booking link; only the route searches remain. Alerts never use an
  expired quote (section 4).
* **Per-user daily budget.** `FLIGHT_USER_DAILY_QUOTA` (20) provider searches per account
  and UTC day (Redis counter `quota:flights-user:<user>:<YYYYMMDD>`, the `consume_quota`
  pattern; `flights/budget.py`). Cache hits are free; fare re-checks are not counted. When
  it runs out mid-match the match keeps what was found, or waits in `pending_flights`
  ("daily flight-search limit reached …") and is searched by the first flight run after
  00:00 UTC; "Re-check flights" is refused with HTTP 429 `flight_budget_reached`. When
  Redis is down the budget fails open (the provider quota below still applies).
* **Provider quota.** `FLIGHT_DAILY_QUOTA` (300) calls per provider and UTC day (Redis).
  When it runs out the run stops early and reports `quota_exhausted`; the matches wait.
* **Order and fairness.** `queue_order` serves users round-robin (every user's best match
  before anyone's second), each user's matches with explicit requests first, then
  short-notice swells (section 5), then the overall score.
* **Gateway fallback.** Several target-market spots list a small regional airport first
  (Lakey Peak BMU, G-Land BWX, Desert Point LOP, Bocas del Toro BOC, Puerto Escondido PXM;
  Pavones' GLF). When such an airport returns **no offers at all**, the trip is searched
  once more via the international gateway the spot's own airport list names (DPS, SJO,
  LIR, PTY, MEX; `flights/airports.py`), within the search's transfer limit and with the
  gateway's longer transfer for the arrival window (first-choice dates only). The match then
  says "no offers to BMU; searched via DPS". Over-budget offers at the regional airport are
  a real answer and do not trigger the fallback.
* **Operating carriers.** Each stored segment keeps the operating carrier's code and full
  name (Duffel: `slices[].segments[].operating_carrier.name`; Amadeus: the carrier
  dictionary). The opportunity page shows "Operated by …" on the first screen of every
  offer and per segment; alerts list them. Duffel requires this for US regulation:
  "When presenting offers to your customers, you should always show the full name of the
  operating carrier of each segment" (https://duffel.com/docs/api/offer-requests).
* **Currency, never guessed.** Budgets are per traveller in the search currency. An offer
  quoted in another currency (Duffel prices in the organisation's billing currency,
  https://duffel.com/docs/api/offers) is converted only with `FX_RATES_USD_JSON` rates and
  otherwise rejected ("quoted in USD (no FX rate configured)"). A converted price is always
  labelled: the opportunity page shows "converted from $812 with a configured exchange
  rate", the dashboard "converted from USD", the email names the provider's own fare.

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

`surf = 0.7 × peak score + 0.3 × average window score`. When the search has a preferred
wave height (`wave_preferred_ft`), `surf = 0.6 × peak + 0.25 × average + 15 × fit`, where
`fit` (0-1) is how close the window's breaking height (middle of its likely faces) is to the
preference, relative to the farther end of the accepted range (`height_fit`). The surf part
is combined with confidence, affordability and convenience; weights per priority (override
with `RANKING_WEIGHTS_JSON`):

| Priority | Surf | Affordability | Confidence | Convenience |
|----------|-----:|--------------:|-----------:|------------:|
| balanced | 0.40 | 0.25 | 0.15 | 0.20 |
| best_waves | 0.60 | 0.15 | 0.15 | 0.10 |
| cheapest | 0.25 | 0.50 | 0.10 | 0.15 |
| shortest | 0.25 | 0.15 | 0.10 | 0.50 |

Surf-only matches have no flight components; the weights renormalise over the rest. These
are heuristic indices, not probabilities.

### Booking links

Offers link to the provider's own booking flow when it returns one ("Book with …"; never
for an expired quote). Otherwise, and always as an alternative, they link to an ordinary
Google Flights / Skyscanner search URL for the same route and dates: labelled **"route
search — fares may differ"** in the app and in emails (`links.ROUTE_SEARCH_NOTE`). No
scraping and no private endpoints: these are plain links the user clicks.

## 4. Alerts (`alerts.generate`)

| Kind | When |
|------|------|
| `new_opportunity` | first alert with a fare, or flights appeared for a surf-only match |
| `surf_only` | first alert with no qualifying fare, if the search opted into surf-only alerts |
| `improved` | peak label went up, or peak score rose ≥ `ALERT_SCORE_IMPROVEMENT` (10) |
| `price_drop` | same currency, drop ≥ `ALERT_PRICE_DROP_PCT` (10 %) **and** ≥ `ALERT_PRICE_DROP_MIN_AMOUNT` (50) |
| `schedule_change` | recommended arrival or departure moved by ≥ 2 days |

New model runs therefore alert only on a new match or a material change (spec 29).
Follow-ups (everything after the first alert) only go out when the search has "notify on
updates" enabled. Alerts pause once the forecast run is older than twice its source's
stale-after age (`STALE_ALERT_FACTOR`).

**Fare re-check right before sending.** The order is: decide the alert kind → the user's
channels → the daily cap → only then re-check the fare with the provider (Duffel
`GET /air/offers/{id}`, Amadeus Flight Offers Price; at most every 2 h per offer) and decide
the kind again with the current fare. A match with nothing new to say is never re-priced, a
withheld alert (email not verified, cap reached) is never re-priced
(`test_fares_are_rechecked_only_right_before_an_alert_is_sent`). When the fare is no longer
usable (expired, sold out, above the budget) the trip is searched once more right there
(`research_match`: cached searches reused, the user's budget applies) and the alert goes out
with the fresh fare, or not at all; a provider error leaves the match `pending_flights` for
the next flight run. An expired quote is never alerted.

**Content (spec 29).** Spot and country; swell dates in the spot's time zone with how far
ahead they start ("starts in about 4 days, short notice"); predicted quality (x/10);
breaking waves (our estimate and its likely range) and the offshore swell; wind relation,
speed and direction (FROM); forecast confidence with the lead; research confidence (how well
the break is documented) as a separate row, never merged with the forecast confidence
(spec 20; the old combined "confidence at this break" row is gone); model runs and data
credits; suggested travel dates and buffers; the fare per traveller (MOCK FARE label for the
demo provider; the provider's own fare when converted); the itinerary's flight times (local
at each airport); departure and destination airports with names and the ground transfer;
airline and "Operated by"; flight time; when the price was checked; the provider booking
link when present; Google Flights and Skyscanner route searches labelled "route search —
fares may differ"; dashboard, manage and unsubscribe links. The disclaimer says forecasts
get less certain the further ahead they look (no fixed day-5 cut-off).

**De-duplication.** `notifications.dedup_key` is unique. It hashes
(user, swell event, channel, kind, fingerprint), where the fingerprint covers quality
label, price bucket, travel dates and origin. Consequences:

* re-running the job, or two workers racing, cannot queue the same alert twice;
* two overlapping saved searches of one user that find the same swell produce one alert,
  not two.

Each user's "Maximum alerts per day" notification preference (default 10; the Settings
page offers 1, 3, 5, 10 or 20, the API accepts 1-100) caps the volume; capped alerts are
recorded as suppressed. Pausing or deleting a search marks its queued notifications
`skipped`. Delivery also re-checks at send time that the opportunity still exists and its
search is active.

## 5. Short notice (spec 28)

Initial markets: Australia → Bali/Indonesia (DPS, LOP, BWX, BMU, PDG) and the USA → Central
America (SJO, LIR, GLF, LIO, SAL, BOC, PTY, PXM). Swells starting `SHORT_NOTICE_MIN_DAYS`
to `SHORT_NOTICE_MAX_DAYS` (3 to 7) days ahead are prioritised, and the full horizon is
still shown:

* **flight-search order**: short-notice matches come before later ones (after explicit
  requests), so a limited quota goes to the trips that must be booked first;
* **dashboard**: `/api/opportunities` ranks trips with flights first and, within them,
  adds `SHORT_NOTICE_BOOST` (15 points) to the overall score of short-notice swells for
  ordering only (the stored and shown score never changes with time); the dashboard lists
  them under "Short notice" (`MatchSummary.short_notice`) and the rest under "Further
  ahead";
* **wizard**: the "Alert lead time" fields (Travel availability step) say that forecasts
  get less certain the further ahead they look and that 2-5 days out is usually firmer.

## 6. Delivery (`notifications.deliver`)

* Queued rows are claimed with `SELECT … FOR UPDATE SKIP LOCKED` (concurrent workers never
  double-send).
* Rows stuck in `sending` after `NOTIFICATION_SENDING_TIMEOUT_MINUTES` are reclaimed.
* **Idempotency key.** Every email carries one key per message we record:
  `notification-<id>` for alerts and `account-email-<id>` for verification, reset and test
  emails. Resend receives it as the `Idempotency-Key` request header ("Resend checks
  whether an email with the same idempotency key has already been sent in the last 24
  hours", https://resend.com/docs/dashboard/emails/idempotency-keys), so neither an HTTP
  retry after a timeout nor a reclaimed `sending` row sends a duplicate there. SendGrid,
  SMTP and the console have no such key: a rare duplicate stays possible there (preferred
  to a lost alert).
* Preferences, verified email / phone and opt-outs are re-checked at send time.
* Transient provider errors retry with exponential backoff up to
  `NOTIFICATION_MAX_ATTEMPTS`. Permanent errors (invalid number, unsubscribed) fail
  immediately with the reason stored.
* Email carries a one-click unsubscribe link (RFC 8058 `List-Unsubscribe` +
  `List-Unsubscribe-Post` headers and a footer link).
* SMS honours Twilio STOP via the inbound-message webhook (`/api/webhooks/twilio/sms`,
  signature-verified).

## 7. Flight providers: status (researched 2026-10-27)

| Provider | Status in the app | Notes |
|----------|-------------------|-------|
| **Demo** (`FLIGHT_PROVIDER=demo`, default) | Works; clearly labelled mock fares | Fictional carriers D1-D5 ("Demo Air"), real airport time zones; every offer `is_mock`, labelled "MOCK FARE — not a real price" in emails and with a mock-fares badge and note in the app. This is the clearly labelled test adapter spec 36 allows until real credentials exist. |
| **Duffel** (`FLIGHT_PROVIDER=duffel`) | Adapter kept, tested with mocked HTTP only | Self-serve, test mode with `duffel_test_` tokens (sandbox airline, unrealistic prices), live mode after KYC. Its Services Agreement (17 Jun 2026) forbids use "for metasearch purposes (including to build a metasearch on top of the Duffel Platform …)" (https://duffel.com/services-agreement): showing Duffel fares and linking out to other sites to book is at risk under that clause. **Get Duffel's written confirmation before using it in production**, or use it as a seller (Orders, or Duffel Links: https://duffel.com/docs/guides/duffel-links). Costs: $0.005 per search above a 1500:1 search-to-order ratio, and "zero Orders shall be treated as one Order" (https://duffel.com/pricing, services agreement): a search-only app gets about 1,500 free searches a period. Offers expire in about 30 minutes (https://duffel.com/docs/api/offers); 429 `rate_limit_exceeded` with `ratelimit-reset` (https://duffel.com/docs/api/overview/errors), no numeric limit published. |
| **Amadeus** (`FLIGHT_PROVIDER=amadeus`) | Adapter kept, Enterprise only, untested | The Self-Service portal was decommissioned on 2026-07-17 according to secondary sources (https://ignav.com/docs/amadeus-self-service-shutdown, https://airlabs.co/amadeus-self-service-api-shutdown, https://apis.io/plans/amadeus/amadeus-plans-pricing/); developers.amadeus.com/self-service now redirects to the portal root. Enterprise access is by contract; whether it keeps the former endpoint shapes is unverified. |
| Travelpayouts / Aviasales Data API | Not built (affiliate option) | Free with an affiliate token; serves **cached** prices other users found (https://travelpayouts-data-api.readthedocs.io/), round trips supported, affiliate booking links. Its real-time search API forbids searches the user did not start ("Each search query must be initiated by the user", https://travelpayouts.github.io/slate/), so it cannot drive background matching. |
| KAYAK Flights / Price Insights | Not built (affiliate option) | Affiliate network; free Sandbox, production keys after use-case approval (https://affiliates.kayak.com/apis, https://developers.kayak.com/flights-price-insights-api). |
| Skyscanner Travel API | Not built (affiliate option) | For "an established business with a large audience", reviewed on application (https://www.partners.skyscanner.net/product/travel-api). |
| Kiwi.com Tequila | Not an option | Invitation-only since May 2024 (https://media.kiwi.com/articles-and-interviews/better-for-business-kiwi-com-takes-a-new-approach-to-partnerships/). |
| Google Flights | **Never scraped** | No public API since QPX Express closed on 2018-04-10 (https://www.engadget.com/2017-11-01-google-flight-data-api-shutdown.html). Google's terms forbid automated access against robots.txt (https://policies.google.com/terms), and robots.txt disallows `/travel/flights/search`. Scraping services (SerpApi and the like) are not used. The app only shows plain route-search links the user clicks. |

Not verified (no credentials): live Duffel coverage and fares for SYD/MEL/PER → DPS and
LAX/SFO/IAH → SJO/LIR/PTY, whether the regional carriers of the small airports (Wings Air,
Lion Air, Air Panama, Sansa) are bookable through any of these APIs (hence the gateway
fallback), and whether a provider's terms allow sharing one cached quote between users
(the shared cache assumes they do; confirm with the provider before going live).
