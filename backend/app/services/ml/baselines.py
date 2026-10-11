"""Reference forecasts every candidate is scored against.

* raw: the unmodified model Hs (``model_hs_m``). A correction is only worth deploying
  if it beats this on held-out data.
* persistence: the observation at or before the cycle time (<= 1 h old) persisted to
  every valid time of that cycle. It uses only information available at issue time
  (research notes, validation section: persistence is the reference for days 0-2).
* climatology: the monthly mean observed Hs of the station, computed from the TRAINING
  period only (the reference for days 5-10). Months with fewer than
  ``MIN_CLIMATOLOGY_OBS`` distinct training observations have no climatology (NaN), so
  skill vs climatology is computed on the rows where it exists and its sample size is
  reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

MIN_CLIMATOLOGY_OBS = 24


def raw_prediction(frame: pd.DataFrame) -> np.ndarray:
    return frame["model_hs_m"].to_numpy(dtype=float)


def persistence_prediction(frame: pd.DataFrame) -> np.ndarray:
    return pd.to_numeric(frame["persistence_hs_m"], errors="coerce").to_numpy(dtype=float)


@dataclass
class Climatology:
    """Monthly mean observed Hs per station (station_id, month) -> (mean, count)."""

    table: dict[tuple[int, int], tuple[float, int]] = field(default_factory=dict)
    period_start: str | None = None
    period_end: str | None = None

    @classmethod
    def fit(cls, train: pd.DataFrame) -> Climatology:
        """Fit from training rows; each observation counts once however many forecasts
        (cycles x leads) it was paired with."""
        if train.empty:
            return cls()
        obs = train[["station_id", "obs_observed_at", "obs_hs_m"]].dropna()
        obs = obs.drop_duplicates(["station_id", "obs_observed_at"])
        months = pd.to_datetime(obs["obs_observed_at"], utc=True).dt.month
        grouped = obs.assign(month=months).groupby(["station_id", "month"])["obs_hs_m"]
        stats = grouped.agg(["mean", "count"])
        table = {
            (int(sid), int(month)): (float(row["mean"]), int(row["count"]))
            for (sid, month), row in stats.iterrows()
            if int(row["count"]) >= MIN_CLIMATOLOGY_OBS
        }
        times = pd.to_datetime(obs["obs_observed_at"], utc=True)
        return cls(
            table,
            times.min().isoformat() if len(times) else None,
            times.max().isoformat() if len(times) else None,
        )

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        if frame.empty or not self.table:
            return np.full(len(frame), np.nan)
        months = pd.to_datetime(frame["valid_time"], utc=True).dt.month.to_numpy()
        stations = frame["station_id"].to_numpy()
        return np.array(
            [
                self.table.get((int(s), int(m)), (np.nan, 0))[0]
                for s, m in zip(stations, months, strict=True)
            ],
            dtype=float,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "entries": len(self.table),
            "min_obs_per_month": MIN_CLIMATOLOGY_OBS,
            "period_start": self.period_start,
            "period_end": self.period_end,
        }


def add_reference_predictions(frame: pd.DataFrame, climatology: Climatology) -> pd.DataFrame:
    """Copy of ``frame`` with ``pred_raw``, ``pred_persistence`` and ``pred_climatology``."""
    out = frame.copy()
    out["pred_raw"] = raw_prediction(out)
    out["pred_persistence"] = persistence_prediction(out)
    out["pred_climatology"] = climatology.predict(out)
    return out
