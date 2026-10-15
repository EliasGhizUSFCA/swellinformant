# Spot timeline fixtures

| File | What it is | Captured |
|---|---|---|
| `jbay_blend_3h.json` | The first 40 stored rows (prediction + wave forecast) of the real blend run `blend:6e67a441174f999b` for `jeffreys-bay` (3-hourly, Africa/Johannesburg), as this app stored them from NOAA GFS/GFS-Wave 2026-10-11 18Z (read directly from NOAA open data) and ECMWF IFS/ecWAM open data 2026-10-11 12Z, scored with algorithm 2.0.0. | Extracted 2026-10-14 from a scratch clone of the `swell_live2` capture (read-only). |

The `components` dict is trimmed to the keys the timeline reads (factor fits, gates and the
breaking breakdown's partitions) and floats are rounded to 3 decimals. Everything else is as
stored.

Licences: NOAA GFS and GFS-Wave output is public domain (U.S. Government work). ECMWF open
data is CC BY 4.0, (c) ECMWF. The predictions, scores and texts were computed by this
application.

`tests/timeline_fixtures.py` loads the rows. The cadence and time-zone tests (3-hourly then
6-hourly, Europe/Lisbon and Pacific/Auckland clock changes, polar and date-line spots) place
these real values at made-up valid times; those series are synthetic and exist only in tests.
