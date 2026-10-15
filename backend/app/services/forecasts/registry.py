"""Build forecast providers from configuration."""

from __future__ import annotations

from app.core.config import Settings, get_settings
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

# FORECAST_PROVIDERS token -> source code.
SOURCE_CODES = {
    "open_meteo": OPEN_METEO_NOAA_CODE,
    "open_meteo_ecmwf": OPEN_METEO_ECMWF_CODE,
    "windy": WindyProvider.code,
    "demo": DemoForecastProvider.code,
    "noaa_gfs": NOAA_GFS_CODE,
    "ecmwf_ifs": ECMWF_IFS_CODE,
}


def configured_source_codes(settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    return [SOURCE_CODES[name] for name in settings.forecast_provider_list if name in SOURCE_CODES]


def build_provider(
    name: str,
    settings: Settings | None = None,
    overrides: list[SwellOverride] | None = None,
) -> ForecastProvider:
    settings = settings or get_settings()
    if name == "open_meteo":
        return OpenMeteoProvider(settings)
    if name == "open_meteo_ecmwf":
        return OpenMeteoEcmwfProvider(settings)
    if name == "windy":
        if not settings.windy_terms_acknowledged:
            # The settings validator refuses this already; kept for settings built elsewhere.
            raise PermanentProviderError(
                "Windy's API terms forbid storing its data and building derived databases: set "
                "WINDY_TERMS_ACKNOWLEDGED=true only with a licence that allows this use "
                "(docs/API_SETUP_REQUIRED.md)"
            )
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
