"""Source dependency graph: which forecast sources are independent evidence.

Two sources in the same *family* run the same wave model with the same forcing (NOAA
GFS-Wave read straight from NOAA, via Open-Meteo ``ncep_gfswave*`` or via Windy
``gfsWave``). Their agreement says nothing about forecast correctness, so the blend keeps
only one member per family and the agreement check only compares across families.

*Related* families share part of the chain: GEFS-Wave runs the same WAVEWATCH III
physics as GFS-Wave but is forced by the GEFS ensemble; MFWAM and AIFS waves are forced
by, or trained on, ECMWF IFS. Their errors are correlated, which the blend accounts for
with a documented prior correlation until verification data replace it.

References: docs/surf-forecast-model-research.md (source dependency section).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Family:
    code: str
    wave_model: str
    forcing: str
    related_to: tuple[str, ...] = ()
    notes: str = ""


FAMILIES: dict[str, Family] = {
    f.code: f
    for f in (
        Family(
            "ncep_gfs",
            "WAVEWATCH III (NOAA GFS-Wave)",
            "NOAA GFS atmosphere",
            related_to=("ncep_gefs",),
            notes=(
                "NOAA direct (noaa_gfs_direct), Open-Meteo ncep_gfswave016/025 + GFS "
                "(open_meteo_noaa) and Windy gfsWave."
            ),
        ),
        Family(
            "ncep_gefs",
            "WAVEWATCH III (NOAA GEFS-Wave, 31 members)",
            "NOAA GEFS ensemble atmosphere",
            related_to=("ncep_gfs",),
            notes="Same wave physics as GFS-Wave; different (ensemble) forcing.",
        ),
        Family(
            "ecmwf_ifs",
            "ECMWF ecWAM (IFS HRES wave)",
            "ECMWF IFS atmosphere with altimeter assimilation",
            related_to=("ecmwf_aifs", "mfwam"),
            notes=(
                "ECMWF open data direct (ecmwf_ifs_open) and via Open-Meteo ecmwf_wam025 + "
                "ecmwf_ifs025 (open_meteo_ecmwf). Independent of the NOAA chain."
            ),
        ),
        Family(
            "ecmwf_aifs",
            "ECMWF AIFS wave (machine-learned)",
            "ECMWF analyses",
            related_to=("ecmwf_ifs",),
        ),
        Family(
            "mfwam",
            "Météo-France MFWAM",
            "ECMWF IFS winds",
            related_to=("ecmwf_ifs",),
        ),
        Family("dwd_icon", "DWD GWAM / EWAM", "DWD ICON atmosphere"),
        Family("demo", "synthetic", "synthetic", notes="Never combined with real sources."),
        Family("blend", "Swell Travel Agent blend", "derived from the families it combines"),
    )
}

# Prior correlation of forecast errors between two different families. Heuristic values
# (documented in docs/FORECAST_SOURCES.md) used only until buoy verification estimates
# the error covariance; related families share forcing or physics, so their errors are
# assumed more strongly correlated than those of independent systems.
PRIOR_ERROR_CORRELATION_RELATED = 0.6
PRIOR_ERROR_CORRELATION_INDEPENDENT = 0.3


def are_independent(a: str, b: str) -> bool:
    """True when two families share neither wave model nor forcing."""
    if a == b or "unknown" in (a, b):
        return False
    fa, fb = FAMILIES.get(a), FAMILIES.get(b)
    if fa is None or fb is None:
        return False
    return b not in fa.related_to and a not in fb.related_to


def prior_error_correlation(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if are_independent(a, b):
        return PRIOR_ERROR_CORRELATION_INDEPENDENT
    return PRIOR_ERROR_CORRELATION_RELATED
