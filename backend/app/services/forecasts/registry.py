"""Build forecast providers from configuration."""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.services.forecasts.base import ForecastProvider
from app.services.forecasts.demo import DemoForecastProvider, SwellOverride
from app.services.forecasts.open_meteo import OpenMeteoProvider
from app.services.forecasts.windy import WindyProvider

SOURCE_CODES = {
    "open_meteo": OpenMeteoProvider.code,
    "windy": WindyProvider.code,
    "demo": DemoForecastProvider.code,
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
    if name == "windy":
        return WindyProvider(settings)
    if name == "demo":
        return DemoForecastProvider(
            settings.forecast_days, overrides=overrides, natural_swells=settings.demo_natural_swells
        )
    raise ValueError(f"Unknown forecast provider {name!r}")
