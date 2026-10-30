"""The provider contract C1 (app.services.forecasts.base, foundation): the two independent
components of a spot forecast (design D6) and the backward-compatible additions to
ModelRunRef, RunInfo, FetchResult and ForecastProvider.fetch."""

from __future__ import annotations

import importlib
import inspect
from datetime import UTC, datetime

import pytest

from app.services.forecasts import base
from app.services.forecasts.base import (
    ATMOSPHERE,
    COMPONENT_FIELDS,
    COMPONENTS,
    FORECAST_FIELDS,
    WAVE,
    FetchResult,
    ForecastProvider,
    ModelRunRef,
    RunInfo,
    component_of,
)
from app.services.forecasts.demo import DemoForecastProvider

CYCLE = datetime(2026, 10, 27, 6, tzinfo=UTC)


def test_the_two_components_partition_the_forecast_fields_exactly() -> None:
    assert set(COMPONENT_FIELDS) == COMPONENTS == {ATMOSPHERE, WAVE}
    atmosphere, wave = COMPONENT_FIELDS[ATMOSPHERE], COMPONENT_FIELDS[WAVE]
    # Disjoint, no duplicates, and together exactly FORECAST_FIELDS.
    assert not set(atmosphere) & set(wave)
    assert len(atmosphere) == len(set(atmosphere)) and len(wave) == len(set(wave))
    assert sorted(atmosphere + wave) == sorted(FORECAST_FIELDS)
    assert len(atmosphere) + len(wave) == len(FORECAST_FIELDS)


def test_the_atmosphere_holds_the_wind_and_the_pressure() -> None:
    assert COMPONENT_FIELDS[ATMOSPHERE] == (
        "wind_speed_kmh",
        "wind_direction_deg",
        "wind_gust_kmh",
        "pressure_msl_hpa",
    )
    # The wave component keeps FORECAST_FIELDS' order: sea state, swell partitions, wind sea,
    # sea level.
    assert COMPONENT_FIELDS[WAVE] == tuple(
        f for f in FORECAST_FIELDS if f not in COMPONENT_FIELDS[ATMOSPHERE]
    )
    assert COMPONENT_FIELDS[WAVE][0] == "sig_wave_height_m"
    assert "sea_level_m" in COMPONENT_FIELDS[WAVE]
    assert "wind_wave_height_m" in COMPONENT_FIELDS[WAVE]


@pytest.mark.parametrize("field_name", FORECAST_FIELDS)
def test_every_field_belongs_to_one_component(field_name: str) -> None:
    component = component_of(field_name)
    assert field_name in COMPONENT_FIELDS[component]


def test_component_of_refuses_an_unknown_field() -> None:
    with pytest.raises(KeyError, match="not a forecast field"):
        component_of("air_temperature_c")


def test_the_component_mapping_is_read_only() -> None:
    with pytest.raises(TypeError):
        COMPONENT_FIELDS["tide"] = ()  # type: ignore[index]


def test_the_additions_default_to_the_old_behaviour() -> None:
    ref = ModelRunRef("ncep_gfswave025", CYCLE)
    assert ref.component is None and ref.available_at is None
    assert ModelRunRef("ncep_gfs013", CYCLE, None, ATMOSPHERE).component == ATMOSPHERE
    info = RunInfo(run_key="gfswave:2026102706", issued_at=CYCLE)
    assert dict(info.component_keys) == {}
    assert info.estimated is False and info.testing is False
    keyed = RunInfo(
        run_key="k",
        issued_at=CYCLE,
        component_keys={WAVE: "ncep_gfswave025:2026102706"},
        estimated=True,
        testing=True,
    )
    assert keyed.component_keys[WAVE] == "ncep_gfswave025:2026102706"
    assert FetchResult().components == frozenset({WAVE, ATMOSPHERE})
    # The default mapping is read-only, so no two RunInfos can share a mutable default.
    with pytest.raises(TypeError):
        RunInfo("a", CYCLE).component_keys[WAVE] = "x"  # type: ignore[index]


def test_fetch_takes_the_components_wanted_and_providers_keep_working() -> None:
    params = inspect.signature(ForecastProvider.fetch).parameters
    assert list(params) == ["self", "spots", "run", "components"]
    assert params["components"].default is None
    # Every provider in the package accepts the argument (and may ignore it).
    providers = [
        obj
        for module in ("demo", "open_meteo", "open_meteo_ecmwf", "windy")
        for obj in vars(importlib.import_module(f"app.services.forecasts.{module}")).values()
        if inspect.isclass(obj)
        and issubclass(obj, ForecastProvider)
        and obj is not ForecastProvider
    ]
    assert providers
    for cls in providers:
        signature = inspect.signature(cls.fetch)
        assert "components" in signature.parameters, cls
        assert signature.parameters["components"].default is None, cls
    demo = DemoForecastProvider(3, natural_swells=False)
    run = demo.current_run(CYCLE)
    # Positional calls without components (every existing caller) still work.
    assert demo.fetch([], run).components == COMPONENTS
    assert demo.fetch([], run, frozenset({WAVE})).components == COMPONENTS  # cannot split


def test_the_demo_provider_follows_forecast_days(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "forecast_days", 7)
    assert DemoForecastProvider().forecast_days == 7
    assert DemoForecastProvider(4).forecast_days == 4


def test_base_exports_the_component_names() -> None:
    assert base.ATMOSPHERE == "atmosphere" and base.WAVE == "wave"
