"""Build forecast providers from configuration, through the provider manager (design D4).

:func:`build_provider` is what the planner (``sync.detect.build_probes``) and the
acquisition (``forecasts.ingestion``) call for each FORECAST_PROVIDERS token. While the
primary is Windy and :func:`provider_manager.active_provider` says its fallback stands in
(a missing key, or a recorded credential, testing-key, quota or availability failure),
the token ``windy`` builds the fallback provider (Open-Meteo) instead: one primary at a
time, the same normalized records, stored under the fallback's own source. Such a provider
carries ``fallback_from = "windy"`` (also when it is built from its own token meanwhile), so
the refresh can record the version's ``fallback_from``.
"""

from __future__ import annotations

from app.core.config import WINDY_FALLBACK_PROVIDER, Settings, get_settings
from app.services.forecasts import provider_manager
from app.services.forecasts.base import ForecastProvider
from app.services.forecasts.demo import DemoForecastProvider, SwellOverride
from app.services.forecasts.open_meteo import OpenMeteoProvider
from app.services.forecasts.open_meteo_ecmwf import OpenMeteoEcmwfProvider
from app.services.forecasts.windy import WindyProvider
from app.services.http import PermanentProviderError

# Source codes of the direct open-data ingesters (their modules are imported lazily
# because they pull in ecCodes).
NOAA_GFS_CODE = "noaa_gfs_direct"
ECMWF_IFS_CODE = "ecmwf_ifs_open"
GEFS_WAVE_CODE = "noaa_gefs_wave"
OPEN_METEO_NOAA_CODE = OpenMeteoProvider.code  # "open_meteo_noaa", family ncep_gfs
OPEN_METEO_ECMWF_CODE = OpenMeteoEcmwfProvider.code  # "open_meteo_ecmwf", family ecmwf_ifs
WINDY_CODE = WindyProvider.code  # "windy_gfswave", family ncep_gfs

# Names of the sources whose providers live in modules that import ecCodes, as their
# source_info records them (noaa.NoaaGfsFieldsProvider, ensemble.GefsWaveEnsembleProvider),
# for status pages that name a source before its first run is stored. A test keeps them
# equal to the providers'.
KNOWN_SOURCE_NAMES = {
    "noaa_gfs_fields": "NOAA GFS-Wave + GFS global fields",
    GEFS_WAVE_CODE: "NOAA GEFS-Wave ensemble mean and spread (direct from NOAA open data)",
}

# FORECAST_PROVIDERS token -> source code.
SOURCE_CODES = {
    "open_meteo": OPEN_METEO_NOAA_CODE,
    "open_meteo_ecmwf": OPEN_METEO_ECMWF_CODE,
    "windy": WINDY_CODE,
    "demo": DemoForecastProvider.code,
    "noaa_gfs": NOAA_GFS_CODE,
    "ecmwf_ifs": ECMWF_IFS_CODE,
}
# Set on a provider that stands in for another one (read with getattr: providers built
# normally do not have it).
FALLBACK_ATTRIBUTE = "fallback_from"
WINDY_STORAGE_REFUSED = (
    "Windy's API terms forbid storing its data and building derived databases: set "
    "WINDY_STORAGE_AUTHORIZED=true (formerly WINDY_TERMS_ACKNOWLEDGED) only with a licence "
    "that allows this use (docs/WINDY_API_SETUP.md)"
)


def configured_source_codes(settings: Settings | None = None) -> list[str]:
    """Source codes of the configured providers (the settings selection), priority order."""
    settings = settings or get_settings()
    return [SOURCE_CODES[name] for name in settings.forecast_provider_list if name in SOURCE_CODES]


def active_provider_tokens(settings: Settings | None = None) -> list[str]:
    """The provider tokens to build now (provider_manager.active_provider): the configured
    ones, with Windy replaced by its fallback while that stands in."""
    return list(provider_manager.active_provider(settings or get_settings()).providers)


def active_source_codes(settings: Settings | None = None) -> list[str]:
    """Source codes of :func:`active_provider_tokens`."""
    return [SOURCE_CODES[t] for t in active_provider_tokens(settings) if t in SOURCE_CODES]


def fallback_origin(provider: ForecastProvider) -> str | None:
    """The provider ``provider`` stands in for (``"windy"``), or None."""
    origin = getattr(provider, FALLBACK_ATTRIBUTE, None)
    return str(origin) if origin else None


def _stand_in(name: str, settings: Settings) -> tuple[str, str] | None:
    """(token to build, provider it stands in for) while a fallback is active, else None.

    Only for the Windy primary and its fallback token; Redis is read only then."""
    if name not in ("windy", WINDY_FALLBACK_PROVIDER):
        return None
    selection = settings.provider_selection()
    if selection.primary != "windy" or selection.fallback is None:
        return None
    active = provider_manager.active_provider(settings)
    if active.fallback_from != "windy":
        return None
    return (selection.fallback if name == "windy" else name), "windy"


def build_provider(
    name: str,
    settings: Settings | None = None,
    overrides: list[SwellOverride] | None = None,
) -> ForecastProvider:
    """The provider of FORECAST_PROVIDERS token ``name``, or the active fallback standing in
    for a failing Windy primary (see the module docstring)."""
    settings = settings or get_settings()
    stand_in = _stand_in(name, settings)
    if stand_in is not None:
        token, origin = stand_in
        provider = _build(token, settings, overrides)
        setattr(provider, FALLBACK_ATTRIBUTE, origin)  # noqa: B010 (an optional attribute)
        return provider
    return _build(name, settings, overrides)


def _build(
    name: str, settings: Settings, overrides: list[SwellOverride] | None
) -> ForecastProvider:
    if name == "open_meteo":
        return OpenMeteoProvider(settings)
    if name == "open_meteo_ecmwf":
        return OpenMeteoEcmwfProvider(settings)
    if name == "windy":
        if not settings.windy_storage_authorized:
            # The settings validator refuses this already; kept for settings built elsewhere.
            raise PermanentProviderError(WINDY_STORAGE_REFUSED)
        return WindyProvider(settings)
    if name == "demo":
        return DemoForecastProvider(
            settings.forecast_days, overrides=overrides, natural_swells=settings.demo_natural_swells
        )
    if name == "noaa_gfs":
        from app.services.forecasts.noaa import NoaaGfsProvider

        return NoaaGfsProvider(settings)
    if name == "ecmwf_ifs":
        from app.services.forecasts.ecmwf import EcmwfIfsProvider

        return EcmwfIfsProvider(settings)
    raise ValueError(f"Unknown forecast provider {name!r}")
