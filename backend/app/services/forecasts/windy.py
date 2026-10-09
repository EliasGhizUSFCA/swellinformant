"""Windy Point Forecast API v2 (licensed, key-based, official API).

https://api.windy.com/point-forecast/docs — requires a paid Point Forecast API key
(WINDY_API_KEY). Trial keys return randomised data, so never use one for real alerts.
One request per spot per model: ``gfsWave`` for waves, ``gfs`` for wind/pressure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.services.forecasts.base import (
    FetchResult,
    ForecastProvider,
    ForecastRecord,
    RunInfo,
    SourceInfo,
    SpotPoint,
    nominal_gfs_cycle,
)
from app.services.geo import wind_from_uv
from app.services.http import (
    PermanentProviderError,
    ProviderError,
    request_with_retries,
)
from app.services.units import ms_to_kmh

URL = "https://api.windy.com/api/point-forecast/v2"

WAVE_KEYS = {
    "waves_height-surface": "sig_wave_height_m",
    "waves_period-surface": "wave_period_s",
    "waves_direction-surface": "wave_direction_deg",
    "swell1_height-surface": "primary_swell_height_m",
    "swell1_period-surface": "primary_swell_period_s",
    "swell1_direction-surface": "primary_swell_direction_deg",
    "swell2_height-surface": "secondary_swell_height_m",
    "swell2_period-surface": "secondary_swell_period_s",
    "swell2_direction-surface": "secondary_swell_direction_deg",
    "windWaves_height-surface": "wind_wave_height_m",
    "windWaves_period-surface": "wind_wave_period_s",
    "windWaves_direction-surface": "wind_wave_direction_deg",
}


def _at(payload: dict[str, Any], key: str, index: int) -> float | None:
    series = payload.get(key) or []
    value = series[index] if index < len(series) else None
    return float(value) if value is not None else None


class WindyProvider(ForecastProvider):
    code = "windy_gfswave"

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        backoff_seconds: float = 1.0,
    ) -> None:
        self.settings = settings or get_settings()
        if not self.settings.windy_api_key:
            raise PermanentProviderError("WINDY_API_KEY is not configured")
        self.client = client or httpx.Client(timeout=self.settings.forecast_http_timeout_seconds)
        self.backoff_seconds = backoff_seconds

    def source_info(self) -> SourceInfo:
        return SourceInfo(
            code=self.code,
            name="Windy Point Forecast API (GFS-Wave + GFS)",
            provider="windy",
            wave_model="gfsWave",
            atmosphere_model="gfs",
            is_demo=False,
            attribution="Forecast data © Windy.com Point Forecast API",
            license_notes="Requires a paid Windy Point Forecast API key; subject to Windy terms.",
        )

    def current_run(self, now: datetime) -> RunInfo:
        # The point-forecast API does not report the model cycle; use the nominal GFS cycle.
        cycle = nominal_gfs_cycle(now)
        return RunInfo(
            run_key=f"gfsWave:{cycle:%Y%m%d%H}|gfs:{cycle:%Y%m%d%H}",
            issued_at=cycle,
            wave_model_run_at=cycle,
            atmosphere_model_run_at=cycle,
            metadata_source="nominal_cycle",
        )

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        resp = request_with_retries(
            self.client,
            "POST",
            URL,
            provider="windy",
            quota=self.settings.forecast_daily_request_quota,
            backoff_seconds=self.backoff_seconds,
            json={**body, "key": self.settings.windy_api_key},
        )
        if resp.status_code != 200:
            raise PermanentProviderError(f"Windy HTTP {resp.status_code}: {resp.text[:200]}")
        data: dict[str, Any] = resp.json()
        return data

    def fetch(self, spots: list[SpotPoint], run: RunInfo) -> FetchResult:
        result = FetchResult()
        for p in spots:
            try:
                waves = self._post(
                    {
                        "lat": p.forecast_latitude,
                        "lon": p.forecast_longitude,
                        "model": "gfsWave",
                        "parameters": ["waves", "windWaves", "swell1", "swell2"],
                        "levels": ["surface"],
                    }
                )
                result.request_count += 1
                atmos = self._post(
                    {
                        "lat": p.latitude,
                        "lon": p.longitude,
                        "model": "gfs",
                        "parameters": ["wind", "windGust", "pressure"],
                        "levels": ["surface"],
                    }
                )
                result.request_count += 1
            except ProviderError as exc:
                result.errors[p.spot_id] = str(exc)
                continue
            atmos_by_ts: dict[int, dict[str, float | None]] = {}
            for i, ts in enumerate(atmos.get("ts", [])):
                u, v = _at(atmos, "wind_u-surface", i), _at(atmos, "wind_v-surface", i)
                gust, pres = _at(atmos, "gust-surface", i), _at(atmos, "pressure-surface", i)
                row: dict[str, float | None] = {}
                if u is not None and v is not None:
                    speed, direction = wind_from_uv(u, v)
                    row["wind_speed_kmh"] = ms_to_kmh(speed)
                    row["wind_direction_deg"] = direction
                if gust is not None:
                    row["wind_gust_kmh"] = ms_to_kmh(gust)
                if pres is not None:
                    row["pressure_msl_hpa"] = pres / 100.0
                atmos_by_ts[int(ts) // 1000] = row
            records: list[ForecastRecord] = []
            for i, ts in enumerate(waves.get("ts", [])):
                values: dict[str, float | None] = {}
                for key, field in WAVE_KEYS.items():
                    values[field] = _at(waves, key, i)
                if all(v is None for v in values.values()):
                    continue
                epoch = int(ts) // 1000
                values.update(atmos_by_ts.get(epoch, {}))
                records.append(ForecastRecord(datetime.fromtimestamp(epoch, tz=UTC), values))
            if records:
                result.records[p.spot_id] = records
            else:
                result.errors[p.spot_id] = "no wave data returned"
        return result
