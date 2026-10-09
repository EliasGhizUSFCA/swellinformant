# Surf spot seed dataset

Curated seed data for Swell Travel Agent: 50 well-known surf breaks and a table of airports.

| File | Contents |
|---|---|
| `spots.json` | Array of 50 surf spots, each with its physical setup, swell/wind/tide preferences, seasonality, access notes and 1-3 nearby airports. |
| `airports.json` | Array of 153 airport reference rows: every airport referenced by a spot, plus major departure hubs worldwide so users can pick a home airport. |

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
