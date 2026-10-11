"""Features and target of the offshore Hs residual correction.

Every feature is known when the forecast is issued (at the cycle time); nothing is
derived from the observation being predicted or from later data:

=====================  =================================================================
feature                definition (units)
=====================  =================================================================
lead_hours             valid_time - cycle_time (h)
model_hs_m             model significant wave height Hm0 (m), as forecast
log_model_hs           ln(max(model_hs_m, HS_FLOOR_M))
model_tp_s             model PEAK period (s); NaN when the source's period is not Tp
model_dir_sin/cos      sin/cos of the model (peak or mean) wave direction, nautical FROM
swell1_hs_m/tp_s       primary swell partition height (m) and peak period (s)
swell1_dir_sin/cos     sin/cos of the primary swell direction (FROM)
wind_speed_kmh         10 m wind speed (km/h) when the source has it
wind_dir_sin/cos       sin/cos of the wind direction (FROM)
month_sin/cos          month of the valid time on the unit circle, 2π(month-1)/12
hour_sin/cos           UTC hour of the valid time on the unit circle, 2π·hour/24
persistence_hs_m       observed Hs at or before the cycle time (<= 1 h old), else NaN
analysis_log_error     ln(persistence obs) - ln(model Hs at lead 0 of the same cycle)
=====================  =================================================================

Directions enter only as sin/cos components, never as raw degrees. There is no station
identifier: the correction has to transfer to surf spots that have no buoy.

Target (MOS-style residual in log space; research notes, ML section)::

    y = ln(max(obs_hs, HS_FLOOR_M)) - ln(max(model_hs, HS_FLOOR_M))

so the corrected forecast is ``model_hs * exp(ŷ)``. Heights below HS_FLOOR_M are clipped
before taking logs (flat calm readings would otherwise dominate a log-space loss).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

HS_FLOOR_M = 0.05
TARGET_NAME = "log_residual"

FEATURE_NAMES: tuple[str, ...] = (
    "lead_hours",
    "model_hs_m",
    "log_model_hs",
    "model_tp_s",
    "model_dir_sin",
    "model_dir_cos",
    "swell1_hs_m",
    "swell1_tp_s",
    "swell1_dir_sin",
    "swell1_dir_cos",
    "wind_speed_kmh",
    "wind_dir_sin",
    "wind_dir_cos",
    "month_sin",
    "month_cos",
    "hour_sin",
    "hour_cos",
    "persistence_hs_m",
    "analysis_log_error",
)
# Features that need an observation at the cycle time (only at a buoy, or at a spot that
# is paired with one); without them the correction falls back to the raw value when the
# model was trained with them always present (see TrainedCorrection.required_features).
OBSERVATION_FEATURES = ("persistence_hs_m", "analysis_log_error")


def _num(value: Any) -> float:
    if value is None:
        return math.nan
    try:
        out = float(value)
    except (TypeError, ValueError):
        return math.nan
    return out if math.isfinite(out) else math.nan


def direction_components(degrees: Any) -> tuple[Any, Any]:
    """(sin, cos) of nautical FROM directions in degrees (NaN stays NaN)."""
    rad = np.deg2rad(np.asarray(degrees, dtype=float))
    return np.sin(rad), np.cos(rad)


def floored_log(hs: Any) -> Any:
    """ln(max(hs, HS_FLOOR_M)); NaN stays NaN."""
    arr = np.asarray(hs, dtype=float)
    return np.log(np.maximum(arr, HS_FLOOR_M))


def log_residual(obs_hs: Any, model_hs: Any) -> Any:
    """Target: ln(obs) - ln(model), both floored at HS_FLOOR_M."""
    return floored_log(obs_hs) - floored_log(model_hs)


def time_features(valid_time: pd.Series) -> pd.DataFrame:
    """Month and UTC hour of the valid time on the unit circle."""
    vt = pd.to_datetime(valid_time, utc=True)
    month_angle = 2.0 * np.pi * (vt.dt.month.to_numpy(dtype=float) - 1.0) / 12.0
    hours = vt.dt.hour.to_numpy(dtype=float) + vt.dt.minute.to_numpy(dtype=float) / 60.0
    hour_angle = 2.0 * np.pi * hours / 24.0
    return pd.DataFrame(
        {
            "month_sin": np.sin(month_angle),
            "month_cos": np.cos(month_angle),
            "hour_sin": np.sin(hour_angle),
            "hour_cos": np.cos(hour_angle),
        },
        index=valid_time.index,
    )


def build_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Feature matrix (columns = FEATURE_NAMES) from a point-forecast style frame.

    Expected input columns (point_forecasts names): ``lead_hours, hs_m, tp_s,
    period_kind, direction_deg, swell1_hs_m, swell1_tp_s, swell1_direction_deg,
    wind_speed_kmh, wind_direction_deg, valid_time`` and, when available,
    ``persistence_hs_m`` and ``analysis_log_error``. Missing columns become NaN.
    """

    def col(name: str) -> np.ndarray:
        if name not in df.columns:
            return np.full(len(df), np.nan)
        return pd.to_numeric(df[name], errors="coerce").to_numpy(dtype=float)

    hs = col("hs_m")
    tp = col("tp_s")
    if "period_kind" in df.columns:
        tp = np.where(df["period_kind"].to_numpy() == "tp", tp, np.nan)
    else:
        tp = np.full(len(df), np.nan)
    d_sin, d_cos = direction_components(col("direction_deg"))
    s_sin, s_cos = direction_components(col("swell1_direction_deg"))
    w_sin, w_cos = direction_components(col("wind_direction_deg"))
    out = pd.DataFrame(
        {
            "lead_hours": col("lead_hours"),
            # Unfloored, so "raw unchanged" really returns the model value; the floor
            # applies inside the logs only.
            "model_hs_m": hs,
            "log_model_hs": floored_log(hs),
            "model_tp_s": tp,
            "model_dir_sin": d_sin,
            "model_dir_cos": d_cos,
            "swell1_hs_m": col("swell1_hs_m"),
            "swell1_tp_s": col("swell1_tp_s"),
            "swell1_dir_sin": s_sin,
            "swell1_dir_cos": s_cos,
            "wind_speed_kmh": col("wind_speed_kmh"),
            "wind_dir_sin": w_sin,
            "wind_dir_cos": w_cos,
            "persistence_hs_m": col("persistence_hs_m"),
            "analysis_log_error": col("analysis_log_error"),
        },
        index=df.index,
    )
    times = time_features(df["valid_time"]) if "valid_time" in df.columns else None
    for name in ("month_sin", "month_cos", "hour_sin", "hour_cos"):
        out[name] = times[name] if times is not None else np.nan
    return out[list(FEATURE_NAMES)]


def features_from_values(
    *,
    cycle_time: datetime,
    valid_time: datetime,
    hs_m: float | None,
    tp_s: float | None,
    period_kind: str | None,
    direction_deg: float | None,
    swell1_hs_m: float | None = None,
    swell1_tp_s: float | None = None,
    swell1_direction_deg: float | None = None,
    wind_speed_kmh: float | None = None,
    wind_direction_deg: float | None = None,
    persistence_hs_m: float | None = None,
    analysis_log_error: float | None = None,
) -> dict[str, float | None]:
    """One feature row (FEATURE_NAMES -> value or None), identical to the training frame."""
    frame = pd.DataFrame(
        [
            {
                "lead_hours": (valid_time - cycle_time).total_seconds() / 3600.0,
                "hs_m": _num(hs_m),
                "tp_s": _num(tp_s),
                "period_kind": period_kind,
                "direction_deg": _num(direction_deg),
                "swell1_hs_m": _num(swell1_hs_m),
                "swell1_tp_s": _num(swell1_tp_s),
                "swell1_direction_deg": _num(swell1_direction_deg),
                "wind_speed_kmh": _num(wind_speed_kmh),
                "wind_direction_deg": _num(wind_direction_deg),
                "valid_time": pd.Timestamp(valid_time),
                "persistence_hs_m": _num(persistence_hs_m),
                "analysis_log_error": _num(analysis_log_error),
            }
        ]
    )
    row = build_feature_frame(frame).iloc[0]
    return {name: (float(v) if math.isfinite(float(v)) else None) for name, v in row.items()}


def features_from_wave_forecast(
    forecast: Any,
    *,
    cycle_time: datetime,
    persistence_hs_m: float | None = None,
    analysis_log_error: float | None = None,
) -> dict[str, float | None]:
    """Features for one ``wave_forecasts`` row (used by the prediction job).

    Column mapping (wave_forecasts -> feature):

    * ``valid_time - cycle_time`` -> lead_hours, where ``cycle_time`` is the model cycle
      (``forecast_runs.wave_model_run_at``, else ``wave_forecasts.issued_at``)
    * ``sig_wave_height_m`` -> model_hs_m / log_model_hs
    * ``wave_period_s`` -> model_tp_s only when ``wave_period_kind == 'tp'``
    * ``wave_direction_deg`` -> model_dir_sin/cos
    * ``primary_swell_height_m / _period_s / _direction_deg`` -> swell1_*
    * ``wind_speed_kmh / wind_direction_deg`` -> wind_*
    * ``valid_time`` -> month/hour sin/cos
    * persistence_hs_m / analysis_log_error: supplied by the caller from the paired
      station's observation at or before the cycle time (None when not available).

    ``forecast`` may be a WaveForecast ORM object or a mapping with the same keys.
    """

    def get(name: str) -> Any:
        if isinstance(forecast, Mapping):
            return forecast.get(name)
        return getattr(forecast, name, None)

    return features_from_values(
        cycle_time=cycle_time,
        valid_time=get("valid_time"),
        hs_m=get("sig_wave_height_m"),
        tp_s=get("wave_period_s"),
        period_kind=get("wave_period_kind"),
        direction_deg=get("wave_direction_deg"),
        swell1_hs_m=get("primary_swell_height_m"),
        swell1_tp_s=get("primary_swell_period_s"),
        swell1_direction_deg=get("primary_swell_direction_deg"),
        wind_speed_kmh=get("wind_speed_kmh"),
        wind_direction_deg=get("wind_direction_deg"),
        persistence_hs_m=persistence_hs_m,
        analysis_log_error=analysis_log_error,
    )


def feature_matrix(features: Mapping[str, float | None], names: tuple[str, ...]) -> np.ndarray:
    """1 x len(names) float matrix from a feature dict (None -> NaN)."""
    return np.array([[_num(features.get(n)) for n in names]], dtype=float)
