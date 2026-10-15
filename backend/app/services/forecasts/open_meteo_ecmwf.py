"""ECMWF IFS + ecWAM (open data, 0.25 deg) via Open-Meteo: the ``ecmwf_ifs`` family.

FORECAST_PROVIDERS token ``open_meteo_ecmwf``, source ``open_meteo_ecmwf``. The shared
behaviour (cells, native steps, run identity, budget, cache) is in
:mod:`app.services.forecasts.open_meteo`; this module only chooses the models and maps the
variables:

* Waves: Marine API ``models=OPEN_METEO_ECMWF_WAVE_MODEL`` (``ecmwf_wam025``).
  ``wave_peak_period`` is the peak period Tp and becomes ``wave_period_s``
  (``period_kind = "tp"``); ``wave_period`` is ECMWF's mean period (Tm-1,0, ``mwp``: 11.3 s
  against Tp 14.65 s at Pipeline in the audit's live sample) and is kept only as
  ``extra["mean_period_tm10_s"]``, never as the period the scoring reads. ECMWF open data
  have no swell partitions, so none are stored. ``wave_direction`` is the mean direction
  the waves come FROM.
* Atmosphere: ``/v1/ecmwf`` ``models=OPEN_METEO_ECMWF_ATMOS_MODEL`` (``ecmwf_ifs025``):
  10 m wind, gust and MSL pressure at the break's nearest sea cell. Open-Meteo documents
  the gust as "Preceding 3-hour max" (``extra["gust_kind"] = "ecmwf_max_3h"``).
* Runs: only the 00Z and 12Z runs are read, as by the direct ECMWF ingester. They reach
  15 days, while the 06Z and 18Z runs stop at 144 h, and Open-Meteo fills the hours after
  a short run's horizon from the previous run (measured live on 2026-10-14: the 06Z
  ``ecmwf_wam025`` series continued to 2026-10-28 23Z, which only the 00Z run reaches), so
  a 06Z/18Z response is a mix of two runs. The 00Z run is read between its publication
  (about 7 h after 00Z) and the 06Z run's (about 13 h), which every 3-hourly acquisition
  poll covers.
"""

from __future__ import annotations

from typing import ClassVar

from app.services.forecasts.open_meteo import ATTRIBUTION_LINK, OpenMeteoFamilyProvider

ECMWF_LICENCE_URL = "https://creativecommons.org/licenses/by/4.0/"

WAVE = {
    "wave_height": "sig_wave_height_m",
    "wave_direction": "wave_direction_deg",
    "wave_peak_period": "wave_period_s",
    "wave_period": "mean_period_tm10_s",  # to extra only (see the module docstring)
}
EXTRA_ONLY = ("mean_period_tm10_s",)


class OpenMeteoEcmwfProvider(OpenMeteoFamilyProvider):
    code = "open_meteo_ecmwf"
    family = "ecmwf_ifs"
    atmos_path = "/v1/ecmwf"
    gust_kind = "ecmwf_max_3h"
    cycle_hours: ClassVar[tuple[int, ...] | None] = (0, 12)
    max_forecast_days = 15
    period_kind = "tp"

    def wave_models(self) -> list[str]:
        return [self.settings.open_meteo_ecmwf_wave_model]

    def atmos_model(self) -> str:
        return self.settings.open_meteo_ecmwf_atmos_model

    def wave_variables(self, optional: bool) -> dict[str, str]:
        return dict(WAVE)

    def wave_extra(self, row: dict[str, float | None]) -> dict[str, object]:
        return {name: row.get(name) for name in EXTRA_ONLY}

    def display_name(self) -> str:
        return "ECMWF IFS + ecWAM (open data, 0.25°) via Open-Meteo"

    def attribution(self) -> str:
        return (
            f"Weather data by Open-Meteo.com ({ATTRIBUTION_LINK}, CC BY 4.0). Contains modified "
            "ECMWF open data: Copyright European Centre for Medium-Range Weather Forecasts "
            f"(ECMWF), IFS and ecWAM forecasts, CC BY 4.0 ({ECMWF_LICENCE_URL}). Modified: "
            "sampled at Open-Meteo's nearest sea grid cell; only the model's own 3- and "
            "6-hourly steps are stored."
        )
