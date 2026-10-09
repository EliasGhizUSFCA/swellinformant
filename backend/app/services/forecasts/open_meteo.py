"""NOAA GFS (atmosphere) + NOAA GFS-Wave / WAVEWATCH III (waves) via the Open-Meteo API.

Why Open-Meteo: it republishes the NOAA model output as JSON (no GRIB/eccodes tooling),
supports many coordinates per request (one centralised fetch serves every user), and
publishes model-run metadata. The free tier is for non-commercial use; set
OPEN_METEO_API_KEY to use the commercial ``customer-*`` hosts.

Requests per batch of spots (FORECAST_BATCH_SIZE, default 10):
  1. marine  /v1/marine   models=ncep_gfswave025   wave + swell partitions
  2. gfs     /v1/gfs      models=gfs_global          10 m wind, gusts, MSL pressure
  3. marine  /v1/marine   (default model)            sea_level_height_msl (tide proxy)

Variables marked optional are dropped automatically if the API rejects them, and the
sea-level request is best effort — tides are a minor scoring component.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
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
from app.services.http import PermanentProviderError, TransientProviderError, request_with_retries

logger = logging.getLogger(__name__)

WAVE_CORE = {
    "wave_height": "sig_wave_height_m",
    "wave_direction": "wave_direction_deg",
    "wave_period": "wave_period_s",
    "wind_wave_height": "wind_wave_height_m",
    "wind_wave_direction": "wind_wave_direction_deg",
    "wind_wave_period": "wind_wave_period_s",
    "swell_wave_height": "primary_swell_height_m",
    "swell_wave_direction": "primary_swell_direction_deg",
    "swell_wave_period": "primary_swell_period_s",
}
WAVE_OPTIONAL = {
    "secondary_swell_wave_height": "secondary_swell_height_m",
    "secondary_swell_wave_direction": "secondary_swell_direction_deg",
    "secondary_swell_wave_period": "secondary_swell_period_s",
}
ATMOS = {
    "wind_speed_10m": "wind_speed_kmh",
    "wind_direction_10m": "wind_direction_deg",
    "wind_gusts_10m": "wind_gust_kmh",
    "pressure_msl": "pressure_msl_hpa",
}
SEA_LEVEL = {"sea_level_height_msl": "sea_level_m"}

# Open-Meteo "domain" names used by the model metadata endpoint.
META_DOMAINS = {
    "ncep_gfswave025": "ncep_gfswave025",
    "ncep_gfswave016": "ncep_gfswave016",
    "gfs_global": "ncep_gfs025",
    "gfs_seamless": "ncep_gfs025",
    "gfs025": "ncep_gfs025",
}


def _chunks(items: Sequence[SpotPoint], size: int) -> Iterable[list[SpotPoint]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


class OpenMeteoError(PermanentProviderError):
    def __init__(self, reason: str, status: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class OpenMeteoProvider(ForecastProvider):
    code = "open_meteo_noaa"

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        backoff_seconds: float = 1.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or httpx.Client(
            timeout=self.settings.forecast_http_timeout_seconds,
            headers={"User-Agent": "SwellTravelAgent/1.0 (forecast ingestion)"},
        )
        self.backoff_seconds = backoff_seconds
        key = self.settings.open_meteo_api_key
        prefix = "customer-" if key else ""
        self.marine_url = f"https://{prefix}marine-api.open-meteo.com/v1/marine"
        self.gfs_url = f"https://{prefix}api.open-meteo.com/v1/gfs"
        self.marine_meta_host = f"https://{prefix}marine-api.open-meteo.com"
        self.gfs_meta_host = f"https://{prefix}api.open-meteo.com"
        self.wave_model = self.settings.open_meteo_wave_model
        self.atmos_model = self.settings.open_meteo_atmos_model
        self.request_count = 0

    # ------------------------------------------------------------------ metadata
    def source_info(self) -> SourceInfo:
        return SourceInfo(
            code=self.code,
            name="NOAA GFS + GFS-Wave (WAVEWATCH III) via Open-Meteo",
            provider="open_meteo",
            wave_model=self.wave_model,
            atmosphere_model=self.atmos_model,
            is_demo=False,
            attribution="Weather data by Open-Meteo.com (CC BY 4.0); model data NOAA/NCEP.",
            license_notes=(
                "Free tier is non-commercial and rate limited; commercial use requires an "
                "Open-Meteo API subscription (OPEN_METEO_API_KEY)."
            ),
        )

    def _get(self, url: str, params: dict[str, Any]) -> httpx.Response:
        if self.settings.open_meteo_api_key:
            params = {**params, "apikey": self.settings.open_meteo_api_key}
        self.request_count += 1
        return request_with_retries(
            self.client,
            "GET",
            url,
            provider="open_meteo",
            quota=self.settings.forecast_daily_request_quota,
            backoff_seconds=self.backoff_seconds,
            params=params,
        )

    def _model_run_time(self, host: str, model: str) -> datetime | None:
        domain = META_DOMAINS.get(model, model)
        try:
            resp = self._get(f"{host}/data/{domain}/static/meta.json", {})
        except TransientProviderError as exc:
            logger.warning("Open-Meteo metadata unavailable for %s: %s", domain, exc)
            return None
        if resp.status_code != 200:
            logger.warning("Open-Meteo metadata for %s returned HTTP %s", domain, resp.status_code)
            return None
        try:
            ts = resp.json().get("last_run_initialisation_time")
            return datetime.fromtimestamp(int(ts), tz=UTC) if ts else None
        except (ValueError, TypeError):
            return None

    def current_run(self, now: datetime) -> RunInfo:
        wave_run = self._model_run_time(self.marine_meta_host, self.wave_model)
        atmos_run = self._model_run_time(self.gfs_meta_host, self.atmos_model)
        meta_source = "open_meteo_meta"
        if wave_run is None or atmos_run is None:
            # Unavailable metadata (endpoint down or changed): assume the nominal NOAA cycle.
            cycle = nominal_gfs_cycle(now)
            wave_run = wave_run or cycle
            atmos_run = atmos_run or cycle
            meta_source = "nominal_cycle"
        run_key = f"{self.wave_model}:{wave_run:%Y%m%d%H}|{self.atmos_model}:{atmos_run:%Y%m%d%H}"
        return RunInfo(
            run_key=run_key,
            issued_at=min(wave_run, atmos_run),
            wave_model_run_at=wave_run,
            atmosphere_model_run_at=atmos_run,
            metadata_source=meta_source,
        )

    # ------------------------------------------------------------------ data
    def _request_points(
        self,
        url: str,
        lats: list[float],
        lons: list[float],
        variables: list[str],
        extra: dict[str, Any],
    ) -> list[dict[str, Any]]:
        params = {
            "latitude": ",".join(f"{x:.4f}" for x in lats),
            "longitude": ",".join(f"{x:.4f}" for x in lons),
            "hourly": ",".join(variables),
            "forecast_days": self.settings.forecast_days,
            "timezone": "GMT",
            "timeformat": "unixtime",
            **extra,
        }
        resp = self._get(url, params)
        if resp.status_code != 200:
            reason = f"HTTP {resp.status_code}"
            try:
                body = resp.json()
                reason = str(body.get("reason") or reason)
            except ValueError:
                pass
            raise OpenMeteoError(reason, resp.status_code)
        data = resp.json()
        items = data if isinstance(data, list) else [data]
        if len(items) != len(lats):
            raise PermanentProviderError(
                f"Open-Meteo returned {len(items)} locations for {len(lats)} requested"
            )
        return items

    def _fetch_group(
        self,
        url: str,
        points: list[SpotPoint],
        mapping: dict[str, str],
        optional: dict[str, str],
        extra: dict[str, Any],
        use_forecast_point: bool,
    ) -> tuple[dict[int, dict[int, dict[str, float | None]]], dict[int, str]]:
        """Fetch one variable group for a batch; on a location error, isolate per spot."""
        lats = [p.forecast_latitude if use_forecast_point else p.latitude for p in points]
        lons = [p.forecast_longitude if use_forecast_point else p.longitude for p in points]
        variables = {**mapping, **optional}
        try:
            items = self._request_points(url, lats, lons, list(variables), extra)
        except OpenMeteoError as exc:
            if optional and exc.status == 400 and any(v in exc.reason for v in optional):
                logger.info(
                    "Open-Meteo rejected optional variables (%s); retrying without", exc.reason
                )
                return self._fetch_group(url, points, mapping, {}, extra, use_forecast_point)
            if len(points) > 1 and exc.status == 400:
                # One bad location (e.g. outside the model domain) fails the whole batch.
                out: dict[int, dict[int, dict[str, float | None]]] = {}
                errs: dict[int, str] = {}
                for p in points:
                    o, e = self._fetch_group(url, [p], mapping, optional, extra, use_forecast_point)
                    out.update(o)
                    errs.update(e)
                return out, errs
            return {}, {p.spot_id: exc.reason for p in points}
        result: dict[int, dict[int, dict[str, float | None]]] = {}
        for point, item in zip(points, items, strict=True):
            hourly = item.get("hourly") or {}
            times = hourly.get("time") or []
            series: dict[int, dict[str, float | None]] = {}
            for idx, ts in enumerate(times):
                row: dict[str, float | None] = {}
                for api_name, field in variables.items():
                    values = hourly.get(api_name)
                    val = values[idx] if values is not None and idx < len(values) else None
                    row[field] = float(val) if val is not None else None
                series[int(ts)] = row
            result[point.spot_id] = series
        return result, {}

    def fetch(self, spots: list[SpotPoint], run: RunInfo) -> FetchResult:
        result = FetchResult()
        self.request_count = 0
        for batch in _chunks(spots, self.settings.forecast_batch_size):
            try:
                waves, wave_err = self._fetch_group(
                    self.marine_url,
                    batch,
                    WAVE_CORE,
                    WAVE_OPTIONAL,
                    {"models": self.wave_model},
                    use_forecast_point=True,
                )
            except (TransientProviderError, PermanentProviderError) as exc:
                for p in batch:
                    result.errors[p.spot_id] = f"wave data: {exc}"
                continue
            try:
                atmos, atmos_err = self._fetch_group(
                    self.gfs_url,
                    batch,
                    ATMOS,
                    {},
                    {"models": self.atmos_model, "wind_speed_unit": "kmh"},
                    use_forecast_point=False,
                )
            except (TransientProviderError, PermanentProviderError) as exc:
                atmos, atmos_err = {}, {p.spot_id: str(exc) for p in batch}
            try:
                tides, _ = self._fetch_group(
                    self.marine_url, batch, SEA_LEVEL, {}, {}, use_forecast_point=True
                )
            except (TransientProviderError, PermanentProviderError) as exc:
                result.warnings.append(f"sea level unavailable: {exc}")
                tides = {}

            for p in batch:
                if p.spot_id in wave_err or p.spot_id not in waves:
                    result.errors[p.spot_id] = f"wave data: {wave_err.get(p.spot_id, 'missing')}"
                    continue
                if p.spot_id in atmos_err:
                    result.warnings.append(
                        f"{p.slug}: wind data unavailable ({atmos_err[p.spot_id]})"
                    )
                records: list[ForecastRecord] = []
                for ts, wave_vals in sorted(waves[p.spot_id].items()):
                    if all(v is None for v in wave_vals.values()):
                        continue
                    values = dict(wave_vals)
                    values.update(atmos.get(p.spot_id, {}).get(ts, {}))
                    values.update(tides.get(p.spot_id, {}).get(ts, {}))
                    records.append(
                        ForecastRecord(valid_time=datetime.fromtimestamp(ts, tz=UTC), values=values)
                    )
                if records:
                    result.records[p.spot_id] = records
                else:
                    result.errors[p.spot_id] = (
                        "no wave data at forecast point (land or outside model)"
                    )
        result.request_count = self.request_count
        return result
