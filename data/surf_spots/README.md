# Surf spot seed dataset

Curated seed data for Swell Travel Agent: 50 well-known surf breaks and a table of airports.

| File | Contents |
|---|---|
| `spots.json` | Array of 50 surf spots, each with its physical setup, swell/wind/tide preferences, seasonality, access notes and 1-3 nearby airports. |
| `airports.json` | Array of 153 airport reference rows: every airport referenced by a spot, plus major departure hubs worldwide so users can pick a home airport. |
| `research_evidence.json` | Published sources consulted per spot and what each says about the spot's ideal conditions; drives research confidence (see "Research evidence" below). Optional for the app; the committed file covers all 50 spots. |
| `physical_profiles.json` | Measured physical parameters for the nearshore model, currently from NOAA BlueTopo bathymetry for Mavericks, Ocean Beach, Trestles and Rincon (see "Measured bathymetry" below). Optional; spots without an entry use profiles derived from the swell windows. |
| `profile_decisions.json` | One decision record per spot and profile field whose evidence contradicts the profile (changed / kept / disputed, with sources and rationale), plus geometry notes (see "Profile decisions" below). Optional for the app; read by the prediction job (disputed winds) and `research lint`. |

**This is a hand-picked list of famous, travel-worthy waves. It is not an objective "best 50" ranking.** Inclusion and order say nothing about quality.

## De-duplication decisions

The source list had two overlaps, which were merged, and two waves were added to keep the count at 50:

- **Snapper Rocks + Superbank → `snapper-rocks-superbank`.** Snapper Rocks is the top of the Superbank sand point, so they are one wave system and share one forecast.
- **Hossegor + La Gravière → `la-graviere-hossegor`.** La Gravière is one of Hossegor's beaches, and the heaviest; listing both would duplicate the same stretch of coast.
- **Added Raglan (Manu Bay), New Zealand:** the region's best-known wave, and the dataset had no New Zealand spot.
- **Added Mullaghmore Head, Ireland:** a well-established North Atlantic big-wave reef that rounds out the European big-wave coverage.

## Field definitions (`spots.json`)

| Field | Meaning |
|---|---|
| `slug` | Stable identifier (kebab-case). |
| `country_code` | ISO 3166-1 alpha-2. |
| `region` / `region_group` | Local region as free text. `region_group` is one of: Hawaii, North America, Mexico & Central America, South America, Indonesia, South Pacific, Australia & New Zealand, Africa, Europe, Asia. |
| `latitude`, `longitude` | Approximate lineup of the break (WGS84, 4 decimals, roughly 10 m precision, but see the accuracy notes). |
| `forecast_latitude`, `forecast_longitude` | An open-ocean point 14-22 km seaward of the break, in the direction of its main swell exposure, for querying ~25 km global wave models without getting land or sheltered cells (2 decimals). |
| `timezone` | IANA time zone name (valid in Python `zoneinfo`). |
| `break_type` | `reef`, `point`, `beach`, `rivermouth` or `slab`. |
| `wave_direction` | `left`, `right` or `both`, from the surfer's point of view riding toward shore. |
| `is_big_wave` | `true` for dedicated big-wave venues (Waimea, Jaws, Mavericks, Nazaré, Shipstern, Dungeons, Mullaghmore). Heavy but more regularly surfed waves such as Teahupoʻo and Punta de Lobos are `false`. |
| `swell_window_min` / `_max` | **Compass degrees the swell comes FROM.** The full range of swell directions that can reach the break, read **clockwise from min to max**. The window may wrap through north: `min 280, max 20` means 280°→360°→20°. |
| `optimal_swell_direction_min` / `_max` | A narrower ideal range inside the exposure window, using the same clockwise/wrap convention. |
| `offshore_wind_direction` | Compass degrees the wind blows **FROM** when it is offshore at the break. At points and wrapping reefs this is the offshore direction for the wave face, or the conventional "best wind" in surf guides, so it can differ from the straight-line coast normal (for example SE trades at Uluwatu and G-Land, SW at the Gold Coast points). |
| `min_swell_period_s` / `ideal_swell_period_s` | Swell period below which the spot rarely works, and the period it likes best. |
| `tide_preference` | `low`, `low_to_mid`, `mid`, `mid_to_high`, `high`, `all`, or `null` when sources conflict or no preference is established. |
| `wave_height_min_ft` / `_max_ft` | Suggested working range of **breaking face height in feet** (not Hawaiian scale, not deep-water swell height). |
| `height_factor` | **An uncalibrated heuristic, not a measurement.** A rough multiplier of breaking height relative to a fully exposed open beach for the same offshore swell (0.4-1.8). For example: Nazaré 1.6 (canyon focusing), Jaws 1.4, sheltered points such as Rincon 0.6, Mundaka 0.7 and Snapper 0.7, exposed reefs 1.0-1.2. Calibrate against observations before relying on it. |
| `skill_level` | `beginner`, `intermediate`, `advanced` or `expert`: the minimum level at which surfing it is reasonable when it is working. |
| `best_months` | Integers 1-12. |
| `seasonality`, `accessibility_notes`, `hazards`, `description` | Short factual prose. |
| `airports[]` | 1-3 entries. The first is the primary (`is_primary: true`, exactly one per spot): the closest practical airport with scheduled commercial service that travellers actually use. `transfer_minutes` is the realistic door-to-door ground or boat time from that airport to the break. `transfer_mode` is one of `car`, `boat`, `car+boat`, `car+hike`, `domestic_flight+car` or `car+ferry`. |

`airports.json` rows have `iata`, `name`, `city`, `country`, `country_code`, `latitude`, `longitude` (4 decimals) and `timezone` (IANA). IATA codes are unique. The rows are sorted by IATA.

## Accuracy and verification

Coordinates and spot attributes come from domain knowledge, spot-checked with web searches of public surf atlases (mondo.surf, surf-forecast.com), Wikipedia and a University of Hawaiʻi table of surf-spot GPS positions. **Re-verify everything against charts or satellite imagery before any safety-critical or navigational use.** Swell windows, wind directions, heights and seasons are typical values, not guarantees.

**Break coordinates checked against at least one published source:**
Pipeline, Mavericks, Jaws (Wikipedia figure used; mondo.surf lists a point about 3.8 km further east), Teahupoʻo, Cloudbreak (only rounded figures were found, so it is approximate), Macaronis, Rifles, G-Land, Lakey Peak, Keramas, Desert Point (rounded figure only, so approximate), Shipstern Bluff, Punta de Lobos, Chicama, Lobitos, Dungeons, Safi, Anchor Point, Coxos, Nazaré, Kirra (mondo.surf figure rejected as inconsistent; position set at the Kirra Point groyne), The Box, Margaret River Main Break, North Point, Pavones, Salsa Brava, Playa Hermosa, Bocas del Toro (Silverbacks), Puerto Escondido (beach position verified; peak position estimated), Punta Roca (rounded figure only), Cloud 9, Thurso East and Raglan (Manu Bay).

**Estimated without a precise published source (lower confidence):** Skeleton Bay (the sand spit migrates north over time, so position is ±1-2 km), Mullaghmore Head (placed from "about 400 m NW of the village"), Restaurants (placed off the SW tip of Tavarua) and La Gravière.

**Remaining spots** (Sunset, Waimea, Ocean Beach, Trestles, Rincon, Uluwatu, Padang Padang, Bells, Snapper Rocks, Burleigh, Jeffreys Bay, Supertubos, Mundaka) are well known and were set from domain knowledge.

**Attribute details also checked by search:** Rifles (SSW swell, NE offshore, mid-high tide), Mullaghmore (left, SE offshore, mid-high tide), Shipstern (N offshore; sources disagree on tide, so it is `null`), Pavones, Safi and Anchor Point offshore winds, Lobitos tide, Keramas season and tide, and Raglan wind.

**Forecast points** were checked against a 1 km global land mask (GLOBE data via the `global-land-mask` package). All 50 points are over water, at least 10 km from any land, and 14-22 km from their break. Some breaks sit in partly enclosed water, where a coarse wave model may still be affected by nearby land or islands: Rincon (Santa Barbara Channel), Keramas (Badung Strait), Desert Point (Lombok Strait), Thurso East (Pentland Firth approaches) and Pavones (Golfo Dulce mouth).

**Airports:** coordinates are standard published reference positions taken from domain knowledge, not individually re-verified. Two secondary airports have limited or intermittent scheduled service: Wick (WIC) and Limón (LIO). Domestic flights to Bocas del Toro mostly leave from Panama City's Albrook airport (PAC) rather than Tocumen (PTY).

## Research evidence

`research_evidence.json` (optional) records, per spot, the published sources that were actually consulted and what each one says about the profile above: swell direction, swell size, swell period, wind, tide, bottom shape, season and how the wave breaks. The app turns it into **research confidence** (High / Moderate / Low per variable and overall, with plain-language gaps): how well each break's ideal conditions are documented. It is separate from forecast confidence and is not a probability. The rules, the reasoning behind them and the full file format are in [`docs/research-confidence.md`](../../docs/research-confidence.md).

- **Without the file** every spot is assessed Low with a gap for every variable ("Limited information is available about ..."), because the values in `spots.json` above come from domain knowledge and spot checks, not from recorded sources.
- **The committed file** holds two research passes. The first (2026-10-11: 259 sources, 805 claims) came from domain-restricted web searches while direct page access was blocked, so its excerpts are a search tool's text about each page. The second (2026-10-12) re-read every source that could be read: 449 sources, 384 read directly with verbatim excerpts confirmed on the page and every claim judged by an independent verifier, 1,711 claims; the 65 sources behind bot protection are kept from the first pass as `"retrieval": "web_search"`. On 2026-10-13 the research-v1.2 corrections (seven tourism boards reclassified from official to magazine, five values removed) and 19 NOAA BlueTopo bathymetry tiles were added: **468 sources (403 read directly), 1,730 claims**. Research confidence (research-v1.2): 35 spots Moderate, 15 Low, none High. Search-only evidence stays capped at Moderate. Sources are never invented, never cited from memory, and never taken from Surfline (including Magicseaweed, now part of Surfline) or Windy.
- **Tiers**: `official` means a government agency with a coastal or marine mandate, a national surfing reserve, or a government geological or hydrographic survey; tourism boards and World Surf League editorial pages are `magazine`. Sizes are working ranges of breaking face height, never record waves.
- Each source records how it was read: `"retrieval": "fetched"` (the default) when the page itself was read and the excerpt copied verbatim, `"web_search"` when only a search summary was available.
- Each source has a `source_type` (survey data, peer-reviewed, official, guidebook, encyclopedia, surf-forecast-site spot guide, magazine or community) that sets its tier, a publisher (pages of one publisher or one website count as one independent source), the year it was published (if known) and the date it was accessed.
- A claim's normalised `value` uses the conventions of this file: directions in degrees the swell or wind comes **from**, windows clockwise and possibly wrapping through north; sizes as **breaking face height in feet** (never offshore significant wave height or Hawaiian scale); periods in seconds; months 1-12. The app re-checks agreement with the profile in code.
- Load it with `python -m app.cli research load` (also part of `seed` / `init-db`). The loader lists every problem at once and writes nothing while there is one; reloading an unchanged file changes nothing. `python -m app.cli research show <slug>` prints a spot's result.


## Profile decisions

`profile_decisions.json` records, for every spot and profile field (offshore wind direction, optimal swell window, working size range, tide preference, best months) whose current research assessment has contradicting evidence, whether the value was **changed**, **kept** or is **disputed**, which evidence keys the decision rests on and why (`slug`, `field`, `old_value`, `new_value` (set only when changed), `decision`, `sources`, `rationale`, `decided_at`, and optionally `alternative`, `sensitivity`, `model_wind`, `reason`). The rule (ADR-018 in [`docs/decisions.md`](../../docs/decisions.md), [`docs/research-confidence.md`](../../docs/research-confidence.md) §10) is mechanical: a value changes only when at least two independent publishers whose pages were read directly agree on a stated new value and every directly read source supporting the old value is of a strictly lower tier than both of them. Model wind is reported for wind records but never decides.

The 2026-10-13 records: 82 decisions (4 changed, 48 disputed, 30 kept) and 6 geometry notes. Changed in `spots.json`: Ocean Beach, Snapper Rocks and Punta Roca `tide_preference` → `all`; Cloudbreak `wave_height_min_ft` 4 → 2. No offshore wind direction met the rule; eleven are disputed (Chicama, Lobitos, Desert Point, Rifles, Cloudbreak, Jeffreys Bay, Dungeons, Skeleton Bay, Supertubos, Mullaghmore Head, Anchor Point), and the app tells users so on every forecast hour. A publisher that both supports and contradicts a value (one page or sentence each way) does not outweigh its own contradiction, so such cases are disputed, not kept (review of 2026-10-13: Anchor Point's wind, Lobitos' swell window, Waimea Bay's size range).

Geometry notes (`reason`): `wave_face` (at a point or wrapping reef the offshore wind is for the wave face, and a directly read source supports the direction) or `stated_orientation` (a source or a measurement gives the break's orientation). `python -m app.cli research lint` lists spots whose offshore wind is more than 60° from straight offshore without such a record; today only Restaurants.

## Measured bathymetry

`physical_profiles.json` is written by [`scripts/bathymetry_profiles.py`](../../scripts/bathymetry_profiles.py) from NOAA Office of Coast Survey **BlueTopo** (public domain; GeoTIFF tiles with a raster attribute table naming the survey behind every cell, elevations relative to NAVD88). Hawaii is outside BlueTopo's extent. Each value carries `data_quality: "measured"` and the entry a `provenance` object (tiles, delivery dates, SHA-256 checksums, method, plane fits, measured values not applied); `seed` validates the object and keeps it in the file only.

| Spot | Forecast-point depth (m below NAVD88) | Shore normal | Controlling depth |
|---|---:|---|---|
| Mavericks | 57.9 (survey H05395, 1934, interpolated between its soundings) | estimate kept: 84 % of the fitted cells are interpolated between 1929 and 1932 soundings | not set: the break coordinate is 4.3 m below NAVD88, 80 m from the nearest cell at or above 0 m |
| Ocean Beach | 36.4 (survey H12109, 2009, measured depths) | estimate kept: poor plane fit near the beach (R² 0.16) | not set: the coordinate is on the beach (+2.3 m) |
| Trestles | not set: the forecast point lies in NBS Generalization cells (758.7 m there; a best-guess surface outside any survey, kept under `provenance.not_applied`) | **210.9°** measured (94 % measured depths, R² 0.85; estimate was 197.5°) | not set: the coordinate is at the shoreline (+0.5 m) |
| Rincon | 96.5 (survey H09732, 1977, interpolated between its soundings) | estimate kept: the contours face 196°, which would block the documented 270-290° swells in the straight-contour model | not set: the coordinate is on the point (+5.6 m) |

The break coordinates above are shoreline positions, not take-off zones, so no measured depth at the break exists yet; a surveyed lineup position per spot would be needed.
