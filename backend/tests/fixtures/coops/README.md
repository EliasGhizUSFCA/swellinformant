# NOAA CO-OPS captures (real responses)

Tide predictions from the NOAA CO-OPS Data API, recorded on 2026-10-14 between 01:52 and
01:54 UTC during the forecast-upgrade audit (`docs/forecast_upgrade_audit.md`, section 4.4).
Requests: `https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?product=predictions&time_zone=gmt&units=metric&format=json&application=swell_travel_agent_audit`
plus `station`, `begin_date`/`end_date` (or a range), `interval` and `datum` as listed below.
Times in the files are UTC (`time_zone=gmt`), heights metres (`units=metric`). These are
astronomical tide predictions, not observed water levels.

| File | Station | Content |
|---|---|---|
| `hilo_1612668.json` | 1612668 Haleiwa, Waialua Bay (subordinate, reference 1612340) | `interval=hilo`, datum MLLW: 8 high/low waters over 48 h (HTTP 200) |
| `hilo72_1612668.json` | 1612668 | the same over 72 h (11 events) |
| `h_1612668_error.json` | 1612668 | `interval=h` at a subordinate station: HTTP **200** with an error body, `{"error": {"message": "No Predictions data was found. Please make sure the Datum input is valid."}}` |
| `h_9414131.json` | 9414131 Pillar Point Harbor, Half Moon Bay (harmonic) | `interval=h`, datum MLLW, 7 hourly values |
| `msl_9414131.json` | 9414131 | `interval=h`, datum MSL, 4 hourly values; MLLW - MSL = 0.911 m at 02:00Z (0.004 vs -0.907) |
| `h72_9414131.json`, `h72_9411270.json`, `h72_1615680.json`, `h72_9410580.json`, `h72_9414290.json` | Pillar Point, Rincon Island, Kahului, Newport Bay Entrance, San Francisco (Golden Gate), all harmonic | 72 hourly values from 2026-10-14 00:00Z. For 9414131 the values equal `msl_9414131.json` where they overlap (datum MSL); the audit's comparison with Open-Meteo used all five as MSL |
| `stations_tidepredictions.trimmed.json` | 62 stations | `https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json?type=tidepredictions` (captured 2026-10-14 01:52:21 UTC, 3,502 stations, 2,042,754 bytes), trimmed to the stations nearest the US and Central/South Pacific spots (and their reference stations) listed by the audit; `count` is the trimmed count and the top-level `self` field (null in the capture) is dropped; every station object is as served |

Licence: NOAA CO-OPS data are U.S. Government works in the public domain; NOS asks that
attribution be given when the material is reproduced
(https://tidesandcurrents.noaa.gov/disclaimers.html).
