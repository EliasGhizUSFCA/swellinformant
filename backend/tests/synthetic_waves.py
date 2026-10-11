"""SYNTHETIC wave observations and point forecasts for the verification/ML tests.

Everything produced here is invented test data, generated from a seeded random process.
It is not an observation, not a model forecast and not a station position; stations are
named ``synthetic:...`` with provider ``synthetic`` and must never be mixed with real
data. Two cases:

* ``bias``: the "model" over-forecasts by a known multiplicative, lead-dependent factor
  ``bias_factor(lead) = 1.15 + 0.20 * lead / 240`` with log-normal noise that grows with
  lead, so a correction can learn it.
* ``noise``: the "model" equals the latent sea state and the "observation" adds
  independent log-normal noise, so the log residual is independent of every feature and
  no correction can genuinely help.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

SYNTHETIC_PROVIDER = "synthetic"
SYNTHETIC_SOURCE = "synthetic_gfswave"
SYNTHETIC_FAMILY = "ncep_gfs"
START = datetime(2025, 1, 1, tzinfo=UTC)


def bias_factor(lead_hours: np.ndarray | float) -> np.ndarray:
    return 1.15 + 0.20 * np.asarray(lead_hours, dtype=float) / 240.0


@dataclass
class SyntheticCase:
    kind: str
    stations: list[dict[str, Any]]
    observations: pd.DataFrame
    forecasts: pd.DataFrame


def _latent_hs(hours: int, rng: np.random.Generator, phase: float) -> np.ndarray:
    """Hourly latent Hs (m): 9-day swell cycle + seasonal term + AR(1) wobble (synthetic)."""
    t = np.arange(hours, dtype=float)
    ar = np.zeros(hours)
    shocks = rng.normal(0.0, 0.02, hours)
    for i in range(1, hours):
        ar[i] = 0.99 * ar[i - 1] + shocks[i]
    log_hs = (
        np.log(1.6)
        + 0.35 * np.sin(2 * np.pi * t / (24 * 9) + phase)
        + 0.15 * np.sin(2 * np.pi * t / (24 * 365) + phase / 3)
        + ar
    )
    return np.exp(log_hs)


def make_case(
    kind: str = "bias",
    *,
    n_stations: int = 3,
    days: int = 240,
    lead_step: int = 6,
    max_lead: int = 240,
    seed: int = 0,
    start: datetime = START,
    obs_noise: float | None = None,
) -> SyntheticCase:
    if kind not in ("bias", "noise"):
        raise ValueError(kind)
    rng = np.random.default_rng(seed)
    hours = days * 24 + max_lead + 48
    obs_noise = obs_noise if obs_noise is not None else (0.05 if kind == "bias" else 0.25)
    stations, obs_frames, fc_frames = [], [], []
    leads = np.arange(0, max_lead + 1, lead_step, dtype=float)
    for s in range(n_stations):
        code = f"synthetic:station-{s + 1}"
        stations.append(
            {
                "code": code,
                "name": f"Synthetic test station {s + 1}",
                "latitude": -30.0 - s,  # invented coordinates for test rows only
                "longitude": 150.0 + s,
                "metadata": {
                    "synthetic": True,
                    "region": "synthetic-east" if s % 2 == 0 else "synthetic-west",
                    "paired_spots": [f"synthetic-spot-{s + 1}"],
                },
            }
        )
        truth = _latent_hs(hours, rng, phase=1.3 * s)
        times = pd.date_range(start, periods=hours, freq="h", tz="UTC")
        obs_hs = truth * np.exp(rng.normal(0.0, obs_noise, hours))
        obs_frames.append(
            pd.DataFrame(
                {
                    "station_code": code,
                    "observed_at": times,
                    "hs_m": np.round(obs_hs, 4),
                    "hs_kind": "spectral_hm0",
                    "tp_s": np.round(10.0 + 3.0 * np.sin(np.arange(hours) / 50.0), 3),
                    "peak_direction_deg": np.round(
                        (200.0 + 20 * np.sin(np.arange(hours) / 40.0)) % 360, 2
                    ),
                    "qc_flag": "good",
                }
            )
        )
        cycles = np.arange(days) * 24  # one 00Z cycle per day
        cyc = np.repeat(cycles, leads.size)
        lead = np.tile(leads, cycles.size)
        valid_idx = (cyc + lead).astype(int)
        true_at_valid = truth[valid_idx]
        if kind == "bias":
            sigma = 0.04 + 0.12 * lead / 240.0
            model = (
                true_at_valid * bias_factor(lead) * np.exp(rng.normal(0.0, 1.0, lead.size) * sigma)
            )
        else:
            model = true_at_valid.copy()
        tp = 10.0 + 3.0 * np.sin(valid_idx / 50.0)
        direction = (200.0 + 20 * np.sin(valid_idx / 40.0)) % 360
        fc_frames.append(
            pd.DataFrame(
                {
                    "station_code": code,
                    "source_code": SYNTHETIC_SOURCE,
                    "family": SYNTHETIC_FAMILY,
                    "cycle_time": [start + timedelta(hours=int(c)) for c in cyc],
                    "valid_time": [start + timedelta(hours=int(v)) for v in valid_idx],
                    "lead_hours": lead,
                    "hs_m": np.round(model, 4),
                    "tp_s": np.round(tp, 3),
                    "direction_deg": np.round(direction, 2),
                    "swell1_hs_m": np.round(0.8 * model, 4),
                    "swell1_tp_s": np.round(tp + 1.0, 3),
                    "swell1_direction_deg": np.round((direction + 5) % 360, 2),
                    "wind_speed_kmh": np.round(15.0 + 5 * np.cos(valid_idx / 30.0), 2),
                    "wind_direction_deg": np.round((90.0 + 30 * np.cos(valid_idx / 30.0)) % 360, 2),
                    "hs_spread_m": np.nan,
                    "extra": [{"synthetic": True, "period_kind": "tp", "direction_kind": "peak"}]
                    * lead.size,
                }
            )
        )
    return SyntheticCase(
        kind,
        stations,
        pd.concat(obs_frames, ignore_index=True),
        pd.concat(fc_frames, ignore_index=True),
    )


def frames_for_assembly(
    case: SyntheticCase,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[int, tuple[str, dict[str, Any]]]]:
    """(forecasts, observations, stations) shaped like the pairing loaders' output, with
    station ids 1..n, for pure (database-free) dataset assembly."""
    ids = {s["code"]: i + 1 for i, s in enumerate(case.stations)}
    fc = case.forecasts.copy()
    fc.insert(0, "station_id", fc.pop("station_code").map(ids).astype("int64"))
    obs = case.observations.copy()
    obs.insert(0, "station_id", obs.pop("station_code").map(ids).astype("int64"))
    obs["mean_direction_deg"] = np.nan
    stations = {i: (code, dict(case.stations[i - 1]["metadata"])) for code, i in ids.items()}
    for col in ("cycle_time", "valid_time"):
        fc[col] = pd.to_datetime(fc[col], utc=True)
    obs["observed_at"] = pd.to_datetime(obs["observed_at"], utc=True)
    return fc, obs, stations


def insert_case(db: Session, case: SyntheticCase) -> dict[str, int]:
    """Insert the synthetic stations, observations and point forecasts; code -> id."""
    from sqlalchemy import insert

    from app.models import BuoyObservation, BuoyStation, PointForecast

    ids: dict[str, int] = {}
    for s in case.stations:
        station = BuoyStation(
            code=s["code"],
            provider=SYNTHETIC_PROVIDER,
            name=s["name"],
            latitude=s["latitude"],
            longitude=s["longitude"],
            license="synthetic test data",
            attribution="synthetic test data",
            metadata_=s["metadata"],
        )
        db.add(station)
        db.flush()
        ids[s["code"]] = station.id
    obs = case.observations.assign(station_id=case.observations["station_code"].map(ids))
    obs_rows = [
        {
            "station_id": int(r.station_id),
            "observed_at": r.observed_at.to_pydatetime(),
            "hs_m": float(r.hs_m),
            "hs_kind": r.hs_kind,
            "tp_s": float(r.tp_s),
            "peak_direction_deg": float(r.peak_direction_deg),
            "qc_flag": r.qc_flag,
            "dataset": "synthetic",
        }
        for r in obs.itertuples(index=False)
    ]
    fc = case.forecasts.assign(station_id=case.forecasts["station_code"].map(ids))
    fc_rows = [
        {
            "station_id": int(r.station_id),
            "source_code": r.source_code,
            "family": r.family,
            "cycle_time": r.cycle_time,
            "valid_time": r.valid_time,
            "lead_hours": float(r.lead_hours),
            "hs_m": float(r.hs_m),
            "tp_s": float(r.tp_s),
            "direction_deg": float(r.direction_deg),
            "swell1_hs_m": float(r.swell1_hs_m),
            "swell1_tp_s": float(r.swell1_tp_s),
            "swell1_direction_deg": float(r.swell1_direction_deg),
            "wind_speed_kmh": float(r.wind_speed_kmh),
            "wind_direction_deg": float(r.wind_direction_deg),
            "extra": r.extra,
        }
        for r in fc.itertuples(index=False)
    ]
    for i in range(0, len(obs_rows), 5000):
        db.execute(insert(BuoyObservation), obs_rows[i : i + 5000])
    for i in range(0, len(fc_rows), 5000):
        db.execute(insert(PointForecast), fc_rows[i : i + 5000])
    db.commit()
    return ids
