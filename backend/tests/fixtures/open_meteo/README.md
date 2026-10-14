# Open-Meteo captures (real responses, unmodified)

Recorded during the forecast-upgrade audit (`docs/forecast_upgrade_audit.md`) with plain
`curl`, no API key (free, non-commercial endpoints). Times are UTC.

| File | Captured | Request |
|---|---|---|
| `marine.json` | 2026-10-14 01:32:47 | `https://marine-api.open-meteo.com/v1/marine?latitude=21.8,-34.2,58.77&longitude=-158.18,25.0,-3.69&hourly=wave_height,wave_direction,wave_period,wave_peak_period,swell_wave_height,swell_wave_direction,swell_wave_period,swell_wave_peak_period,secondary_swell_wave_height,secondary_swell_wave_direction,secondary_swell_wave_period,tertiary_swell_wave_height,tertiary_swell_wave_direction,tertiary_swell_wave_period,wind_wave_height,wind_wave_direction,wind_wave_period&models=ncep_gfswave025,ncep_gfswave016,ecmwf_wam025,meteofrance_wave&forecast_days=2&timezone=GMT&timeformat=unixtime` (HTTP 200) |
| `weather.json` | 2026-10-14 01:33:44 | `https://api.open-meteo.com/v1/forecast?latitude=21.6655,-34.032,33.3825&longitude=-158.053,24.932,-117.5888&hourly=wind_speed_10m,wind_direction_10m,wind_gusts_10m,pressure_msl&models=gfs_seamless,ecmwf_ifs025&forecast_days=2&timezone=GMT&timeformat=unixtime&wind_speed_unit=kmh` (HTTP 200) |
| `meta.txt` | 2026-10-14 01:34 (file written 01:34:41) | One line per model: `{host}/data/{domain}/static/meta.json` on `api.open-meteo.com` and `marine-api.open-meteo.com`, as `<host> <domain>: <body> HTTP=<status>`. The `marine-api ncep_gfs025` request timed out (curl error 28, `HTTP=000`) and is kept as recorded. |
| `marine_400_no_data.json` | 2026-10-14 01:32:25 | Body of the HTTP 400 answer of `/v1/marine` with `models=ncep_gfswave016` for a point outside that model's domain (about 52.5N..15S): `{"reason":"No data is available for this location","error":true}`. The exact query string of this capture was not recorded. |

The three marine points are the forecast (offshore) points of Pipeline, Jeffreys Bay and
Thurso East (58.77N, outside `ncep_gfswave016`); the three weather points are the breaks of
Pipeline, Jeffreys Bay and Trestles (`data/surf_spots/spots.json`). The responses are JSON
arrays, one object per location.

Licence: Open-Meteo API data are offered under CC BY 4.0
(https://open-meteo.com/en/licence), with a link "Weather data by Open-Meteo.com" to
https://open-meteo.com/ next to the data. The underlying models are NOAA NCEP GFS and
GFS-Wave, ECMWF IFS and WAM (CC BY 4.0) and the Meteo-France wave model. These are
forecasts, not observations.
