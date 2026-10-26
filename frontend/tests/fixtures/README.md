# Frontend test fixtures

| File | What it is | Captured |
|---|---|---|
| `timeline-jbay-3h.json` | The `GET /api/spots/jeffreys-bay/timeline?days=10` response of this app for the real blend run `blend:6e67a441174f999b` (3-hourly, Africa/Johannesburg), cut to its first three local days (2026-10-12 to 2026-10-14, 23 steps); `steps_meta.segments[0].end` was set to the last kept step plus 3 h. The run was built from NOAA GFS/GFS-Wave 2026-10-11 18Z (NOAA open data) and ECMWF IFS/ecWAM open data 2026-10-11 12Z and scored with algorithm 2.0.0. The tide and observation blocks are the real "unavailable" blocks of that response. | Built 2026-10-14 by `services/timeline.spot_timeline(now=2026-10-12T04:00Z)` on a scratch clone of the `swell_live2` capture (read-only). |

Licences: NOAA GFS and GFS-Wave output is public domain (U.S. Government work). ECMWF open
data is CC BY 4.0, (c) ECMWF. The predictions, scores and texts were computed by this
application.

Tests that need other shapes (a tide curve, 6-hourly steps, a daylight-saving day, missing
values) derive them from this payload in the test file itself and label them SYNTHETIC.
