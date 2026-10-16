"""Forecast-upgrade settings (app.core.config): provider tokens, the Windy terms gate, the
map-field cadence parsing and the deprecated single-model Open-Meteo aliases."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import FORECAST_PROVIDER_TOKENS, Settings


def make(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type,call-arg]


def test_existing_and_new_provider_tokens_are_accepted() -> None:
    assert {"demo", "noaa_gfs", "ecmwf_ifs", "open_meteo", "windy"} < FORECAST_PROVIDER_TOKENS
    tokens = ["noaa_gfs", "ecmwf_ifs", "open_meteo", "open_meteo_ecmwf"]
    assert make(forecast_providers=",".join(tokens)).forecast_provider_list == tokens
    with pytest.raises(ValidationError, match="Unknown FORECAST_PROVIDERS"):
        make(forecast_providers="open_meteo,surfline")
    with pytest.raises(ValidationError, match="cannot mix 'demo'"):
        make(forecast_providers="demo,open_meteo_ecmwf")


def test_windy_is_refused_without_the_terms_acknowledgement() -> None:
    with pytest.raises(ValidationError, match="WINDY_TERMS_ACKNOWLEDGED") as refused:
        make(forecast_providers="noaa_gfs,windy")
    assert "terms forbid storing" in str(refused.value)
    acknowledged = make(forecast_providers="noaa_gfs,windy", windy_terms_acknowledged=True)
    assert acknowledged.forecast_provider_list == ["noaa_gfs", "windy"]


def test_defaults_resolve_to_the_documented_values() -> None:
    s = make()
    assert s.open_meteo_gfs_wave_model_list == ["ncep_gfswave016", "ncep_gfswave025"]
    assert s.open_meteo_gfs_atmos_model_name == "gfs_global"
    assert s.map_field_partitions == "on"
    assert s.map_field_step_ranges == [(0, 120, 3), (120, 240, 6)]
    assert s.map_field_cycle_hours == [0, 12] and s.gefs_cycle_hours == [0, 6, 12, 18]
    assert s.map_field_atmos_path("20261013", "12", 6) == (
        "gfs.20261013/12/atmos/gfs.t12z.pgrb2.0p50.f006"
    )
    assert s.ecmwf_wave_param_list == ["swh", "mwd", "pp1d", "mwp"]
    assert s.gefs_stat_field_list == ["HTSGW", "PERPW"]
    assert s.tide_provider_list == ["noaa_coops", "harmonic_model"]
    assert s.noaa_coops_application_name == s.app_name
    assert s.worker_queue_list == ["celery", "bulk"]
    assert s.windy_terms_acknowledged is False and s.wind_gust_scoring == "off"
    # Integration: 21 h, not 12 h or 18 h. With MAP_FIELD_CYCLES=00,12 the served map cycle
    # is normally 17-20 h old before the next one replaces it, so 12 h flagged the layers stale
    # for 5-8 h of every 12 and 18 h for up to 2 h (docs/map-layers.md, Limitations).
    assert s.map_field_stale_after_hours == 21.0
    # An alert above the computed two-year steady state (about 17 GB), not after ~100 days.
    assert s.data_health_database_max_gb == 20.0
    # Declared at integration (point-data read it with getattr until then): on by default.
    assert s.prune_superseded_member_predictions is True
    assert s.forecast_batch_size == 50
    assert make(prune_superseded_member_predictions=False).prune_superseded_member_predictions is (
        False
    )


def test_env_example_matches_the_integration_defaults() -> None:
    """.env.example must not override the code defaults the upgrade changed (an old value
    there wins over the default: FORECAST_BATCH_SIZE=10 meant five requests per model)."""
    env = (Path(__file__).resolve().parents[3] / ".env.example").read_text()
    values = dict(
        line.split("=", 1)
        for line in env.splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert values["FORECAST_BATCH_SIZE"] in ("", "50")
    assert values["MAP_FIELD_STALE_AFTER_HOURS"] in ("", "21")
    assert values["PRUNE_SUPERSEDED_MEMBER_PREDICTIONS"] == "true"
    assert values["DATA_HEALTH_DATABASE_MAX_GB"] == ""
    assert "MAP_FIELD_STEP_HOURS" not in values  # deprecated: documented as a comment only
    assert "FORECAST_PROVIDERS=" in env and "open_meteo,open_meteo_ecmwf" in env


def test_deprecated_open_meteo_aliases_win_only_when_set() -> None:
    old = make(open_meteo_wave_model="ncep_gfswave025", open_meteo_atmos_model="gfs_seamless")
    assert old.open_meteo_gfs_wave_model_list == ["ncep_gfswave025"]
    assert old.open_meteo_gfs_atmos_model_name == "gfs_seamless"
    # Unset, the aliases keep their old defaults for the old adapter and override nothing.
    new = make(open_meteo_gfs_atmos_model="gfs_seamless")
    assert new.open_meteo_wave_model == "ncep_gfswave025"
    assert new.open_meteo_gfs_wave_model_list == ["ncep_gfswave016", "ncep_gfswave025"]
    assert new.open_meteo_gfs_atmos_model_name == "gfs_seamless"


@pytest.mark.parametrize("steps", ["0-120", "0-120:0", "120-0:3", "a-b:3", ""])
def test_malformed_map_field_steps_are_refused(steps: str) -> None:
    with pytest.raises(ValidationError, match="MAP_FIELD_STEPS"):
        make(map_field_steps=steps)


@pytest.mark.parametrize("name", ["map_field_cycles", "gefs_cycles"])
@pytest.mark.parametrize("value", ["03", "00,24", "", "twelve"])
def test_cycles_must_be_gfs_cycle_hours(name: str, value: str) -> None:
    with pytest.raises(ValidationError, match="cycles must be"):
        make(**{name: value})
    assert make(**{name: "06,00"}).model_dump()[name] == "06,00"
