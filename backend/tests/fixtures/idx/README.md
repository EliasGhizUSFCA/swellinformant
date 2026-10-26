# GRIB index files (real, unmodified)

Byte-range indexes of NOAA GFS / GFS-Wave and ECMWF open-data files, recorded on 2026-10-13
at 23:01-23:02 UTC during the forecast-upgrade audit (`docs/forecast_upgrade_audit.md`,
section 4.2). They list every GRIB record with its offset (and ECMWF its length), so record
sizes and download budgets can be computed without downloading any GRIB data.

| Files | Source URL |
|---|---|
| `gfs.t12z.pgrb2.0p25.f{006,120,240}.idx` | `https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261013/12/atmos/gfs.t12z.pgrb2.0p25.f{FFF}.idx` (GFS 2026-10-13 12Z) |
| `gfs.t12z.pgrb2.0p50.f{006,120,240}.idx` | `.../gfs.20261013/12/atmos/gfs.t12z.pgrb2.0p50.f{FFF}.idx` |
| `gfswave.t12z.global.0p25.f{006,120,240}.grib2.idx` | `.../gfs.20261013/12/wave/gridded/gfswave.t12z.global.0p25.f{FFF}.grib2.idx` |
| `20261013000000-{24,150,240}h-oper-fc.index` | `https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/20261013/00z/ifs/0p25/oper/20261013000000-{S}h-oper-fc.index` (IFS 2026-10-13 00Z, atmosphere) |
| `20261013000000-{24,150,240}h-wave-fc.index` | `.../20261013/00z/ifs/0p25/wave/20261013000000-{S}h-wave-fc.index` (ecWAM) |

NOAA `.idx` lines are `n:offset:d=YYYYMMDDHH:VAR:level:forecast:`; a record's size is the next
record's offset minus its own (the last record's size needs the file length, which is not
recorded here). ECMWF `.index` lines are JSON objects with `_offset` and `_length`.

Licences: NOAA GFS and GFS-Wave are U.S. Government works distributed through the NOAA Open
Data Dissemination programme (open to the public; NOAA asks for attribution and that no
endorsement be implied). ECMWF open data: CC BY 4.0 and the ECMWF Terms of Use
(https://www.ecmwf.int/en/forecasts/datasets/open-data).
