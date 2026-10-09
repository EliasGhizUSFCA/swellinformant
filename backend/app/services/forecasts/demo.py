"""Demo forecast provider: deterministic synthetic forecasts for development and tests.

Every record it produces belongs to the ``demo`` forecast source (``is_demo = true``) and
is labelled DEMO in the UI, API and alerts. The generator is physically plausible so
the whole pipeline can be exercised offline:

* swell pulses arrive every ~3.5 days with Gaussian height envelopes; period starts long
  and shortens as the swell ages (frequency dispersion); directions are usually inside
  the spot's exposure window
* diurnal winds: light offshore land breeze at dawn, onshore sea breeze in the afternoon,
  plus a slowly varying synoptic wind
* semidiurnal (M2 + S2) tides
* small run-to-run perturbations that grow with lead time, like real model runs

``SwellOverride`` injects a strong, well-aligned swell with light offshore winds at one
spot (used by the "simulate swell" developer tool and the end-to-end tests).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.forecasts.base import (
    FetchResult,
    ForecastProvider,
    ForecastRecord,
    RunInfo,
    SourceInfo,
    SpotPoint,
)
from app.services.geo import norm_deg, wind_from_uv, window_center, window_width
from app.services.surf_quality.breaking import komar_gaughan_m
from app.services.surf_quality.models import SpotParams
from app.services.units import m_to_ft

SLOT_HOURS = 84.0
HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class SwellOverride:
    spot_slug: str
    start: datetime
    end: datetime
    period_s: float | None = None
    target_breaking_ft: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "spot_slug": self.spot_slug,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "period_s": self.period_s,
            "target_breaking_ft": self.target_breaking_ft,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SwellOverride:
        return cls(
            spot_slug=data["spot_slug"],
            start=datetime.fromisoformat(data["start"]),
            end=datetime.fromisoformat(data["end"]),
            period_s=data.get("period_s"),
            target_breaking_ft=data.get("target_breaking_ft"),
        )


def h0_for_breaking_ft(target_ft: float, period_s: float, height_factor: float) -> float:
    """Invert the Komar–Gaughan breaker formula (bisection) for a target face height."""
    lo, hi = 0.05, 20.0
    for _ in range(50):
        mid = (lo + hi) / 2
        if m_to_ft(komar_gaughan_m(mid, period_s)) * height_factor < target_ft:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _vector(speed: float, from_deg: float) -> tuple[float, float]:
    """Wind 'from' direction → (u, v) motion components."""
    rad = math.radians(from_deg)
    return -speed * math.sin(rad), -speed * math.cos(rad)


@dataclass
class _Pulse:
    peak: datetime
    duration_h: float
    h0_m: float
    period_s: float
    direction: float

    def height(self, t: datetime) -> float:
        dt = (t - self.peak).total_seconds() / 3600.0
        return self.h0_m * math.exp(-0.5 * (dt / (self.duration_h / 4.0)) ** 2)

    def period(self, t: datetime) -> float:
        age = (t - (self.peak - timedelta(hours=self.duration_h / 2))).total_seconds() / 3600.0
        value = self.period_s + 2.0 - 4.0 * age / self.duration_h
        return max(6.0, min(22.0, max(self.period_s - 2.0, min(self.period_s + 2.0, value))))


class DemoSeriesGenerator:
    def __init__(self, spot: SpotParams, run_key: str) -> None:
        self.spot = spot
        self.run_key = run_key
        seed = random.Random(f"{spot.slug}:static")
        self.tide_amp = seed.uniform(0.4, 1.4)
        self.tide_phase = seed.uniform(0, 2 * math.pi)
        self.tide_phase2 = seed.uniform(0, 2 * math.pi)
        self.pressure_phase = seed.uniform(0, 2 * math.pi)
        self.sea_breeze_max = seed.uniform(12, 24)
        self.target_ft = (spot.wave_height_min_ft + spot.wave_height_max_ft) / 2.0

    # ---------------------------------------------------------------- swell
    def _pulse_for_slot(self, k: int, issued_at: datetime) -> _Pulse | None:
        rng = random.Random(f"{self.spot.slug}:{k}")
        if rng.random() > 0.75:
            return None
        slot_start = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(hours=k * SLOT_HOURS)
        peak = slot_start + timedelta(hours=rng.uniform(0, SLOT_HOURS))
        aligned = rng.random() < 0.4
        if aligned:
            direction = norm_deg(
                self.spot.optimal_swell_direction_min
                + rng.uniform(
                    0,
                    window_width(
                        self.spot.optimal_swell_direction_min, self.spot.optimal_swell_direction_max
                    ),
                )
            )
        else:
            direction = norm_deg(
                window_center(self.spot.swell_window_min, self.spot.swell_window_max)
                + rng.choice([-1, 1])
                * rng.uniform(0.3, 0.75)
                * window_width(self.spot.swell_window_min, self.spot.swell_window_max)
            )
        period = max(9.0, min(20.0, self.spot.ideal_swell_period_s + rng.uniform(-4.5, 2.0)))
        breaking_target = self.target_ft * rng.lognormvariate(0.0, 0.35) * rng.uniform(0.6, 1.15)
        h0 = h0_for_breaking_ft(breaking_target, period, self.spot.height_factor)
        duration = rng.uniform(36, 72)
        # Run-to-run perturbation: grows with lead time, deterministic per run.
        lead_days = max(0.0, (peak - issued_at).total_seconds() / 86400.0)
        prng = random.Random(f"{self.spot.slug}:{k}:{self.run_key}")
        h0 *= max(0.5, 1.0 + prng.gauss(0, 0.025 * lead_days))
        peak += timedelta(hours=prng.gauss(0, 0.5 * lead_days))
        return _Pulse(peak, duration, h0, period, direction)

    # ---------------------------------------------------------------- wind
    def _wind(self, t: datetime) -> tuple[float, float]:
        local_hour = (t.hour + t.minute / 60 + self.spot.longitude / 15.0) % 24
        day_rng = random.Random(f"{self.spot.slug}:wind:{t:%Y%m%d}")
        land = day_rng.uniform(4, 10) if local_hour < 9.5 or local_hour > 21 else 0.0
        if 9.5 <= local_hour <= 20:
            breeze = (
                self.sea_breeze_max
                * day_rng.uniform(0.5, 1.1)
                * math.sin(math.pi * (local_hour - 9.5) / 10.5)
            )
        else:
            breeze = 0.0
        # Synoptic wind interpolated between 36 h slots.
        hours = (t - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds() / 3600.0
        k = math.floor(hours / 36.0)
        frac = (hours / 36.0) - k

        def syn(i: int) -> tuple[float, float]:
            r = random.Random(f"{self.spot.slug}:syn:{i}")
            return _vector(r.uniform(0, 26), r.uniform(0, 360))

        (u0, v0), (u1, v1) = syn(k), syn(k + 1)
        w = (1 - math.cos(math.pi * frac)) / 2
        su, sv = u0 + (u1 - u0) * w, v0 + (v1 - v0) * w
        lu, lv = _vector(land, self.spot.offshore_wind_direction + day_rng.uniform(-15, 15))
        bu, bv = _vector(breeze, self.spot.offshore_wind_direction + 180 + day_rng.uniform(-30, 30))
        speed, direction = wind_from_uv(su + lu + bu, sv + lv + bv)
        return speed, direction

    # ---------------------------------------------------------------- series
    def generate(
        self, start: datetime, hours: int, overrides: list[SwellOverride] | None = None
    ) -> list[ForecastRecord]:
        start = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=hours)
        epoch_h = (start - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds() / 3600.0
        k0 = math.floor((epoch_h - 96) / SLOT_HOURS)
        k1 = math.floor((epoch_h + hours + 96) / SLOT_HOURS)
        pulses = [p for k in range(k0, k1 + 1) if (p := self._pulse_for_slot(k, start))]
        mine = [o for o in (overrides or []) if o.spot_slug == self.spot.slug]
        for o in mine:
            period = o.period_s or self.spot.ideal_swell_period_s + 1
            target = o.target_breaking_ft or (
                self.spot.wave_height_min_ft
                + 0.6 * (self.spot.wave_height_max_ft - self.spot.wave_height_min_ft)
            )
            duration_h = max(12.0, (o.end - o.start).total_seconds() / 3600.0)
            pulses.append(
                _PlateauPulse(
                    peak=o.start + (o.end - o.start) / 2,
                    duration_h=duration_h,
                    h0_m=h0_for_breaking_ft(target, period, self.spot.height_factor),
                    period_s=period,
                    direction=window_center(
                        self.spot.optimal_swell_direction_min, self.spot.optimal_swell_direction_max
                    ),
                    start=o.start,
                    end=o.end,
                )
            )
        background_dir = window_center(self.spot.swell_window_min, self.spot.swell_window_max)
        records: list[ForecastRecord] = []
        t = start
        while t < end:
            parts = sorted(((p.height(t), p.period(t), p.direction) for p in pulses), reverse=True)
            parts = [x for x in parts if x[0] > 0.05]
            parts.append((0.35, 8.5, background_dir))
            parts.sort(reverse=True)
            primary = parts[0]
            secondary = parts[1] if len(parts) > 1 else None

            if any(o.start - timedelta(hours=6) <= t <= o.end + timedelta(hours=6) for o in mine):
                local_rng = random.Random(f"{self.spot.slug}:ovr:{t.isoformat()}")
                wind_speed = local_rng.uniform(5, 12)
                wind_dir = norm_deg(self.spot.offshore_wind_direction + local_rng.uniform(-20, 20))
            else:
                wind_speed, wind_dir = self._wind(t)
            ww_h = 0.012 * wind_speed**1.15
            ww_t = 2.5 + 0.1 * wind_speed
            sec_h = secondary[0] if secondary else 0.0
            hs = math.sqrt(primary[0] ** 2 + sec_h**2 + ww_h**2)
            hours_f = (t - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds() / 3600.0
            tide = self.tide_amp * math.cos(2 * math.pi * hours_f / 12.42 + self.tide_phase) + (
                0.3 * self.tide_amp * math.cos(2 * math.pi * hours_f / 12.0 + self.tide_phase2)
            )
            values: dict[str, float | None] = {
                "sig_wave_height_m": round(hs, 2),
                "wave_period_s": round(primary[1], 1),
                "wave_direction_deg": round(primary[2], 0),
                "primary_swell_height_m": round(primary[0], 2),
                "primary_swell_period_s": round(primary[1], 1),
                "primary_swell_direction_deg": round(primary[2], 0),
                "secondary_swell_height_m": round(secondary[0], 2) if secondary else None,
                "secondary_swell_period_s": round(secondary[1], 1) if secondary else None,
                "secondary_swell_direction_deg": round(secondary[2], 0) if secondary else None,
                "wind_wave_height_m": round(ww_h, 2),
                "wind_wave_period_s": round(ww_t, 1),
                "wind_wave_direction_deg": round(wind_dir, 0),
                "wind_speed_kmh": round(wind_speed, 1),
                "wind_direction_deg": round(wind_dir, 0),
                "wind_gust_kmh": round(wind_speed * 1.4, 1),
                "pressure_msl_hpa": round(
                    1013 + 6 * math.sin(2 * math.pi * hours_f / 120 + self.pressure_phase), 1
                ),
                "sea_level_m": round(tide, 2),
            }
            records.append(ForecastRecord(valid_time=t, values=values))
            t += HOUR
        return records


@dataclass
class _PlateauPulse(_Pulse):
    start: datetime = datetime.min.replace(tzinfo=UTC)
    end: datetime = datetime.min.replace(tzinfo=UTC)

    def height(self, t: datetime) -> float:
        if self.start <= t <= self.end:
            return self.h0_m
        edge = self.start if t < self.start else self.end
        dt = abs((t - edge).total_seconds()) / 3600.0
        return self.h0_m * math.exp(-0.5 * (dt / 8.0) ** 2)

    def period(self, t: datetime) -> float:
        return self.period_s


class DemoForecastProvider(ForecastProvider):
    code = "demo"

    def __init__(
        self,
        forecast_days: int = 16,
        overrides: list[SwellOverride] | None = None,
        run_key: str | None = None,
    ) -> None:
        self.forecast_days = forecast_days
        self.overrides = overrides or []
        self.run_key_override = run_key

    def source_info(self) -> SourceInfo:
        return SourceInfo(
            code=self.code,
            name="DEMO synthetic forecast (not real data)",
            provider="demo",
            wave_model="demo-swell-generator",
            atmosphere_model="demo-diurnal-wind",
            is_demo=True,
            attribution="Synthetic demo data generated by Swell Travel Agent",
            license_notes="Not a real forecast. For development and testing only.",
        )

    def current_run(self, now: datetime) -> RunInfo:
        now = now.astimezone(UTC)
        cycle = now.replace(hour=(now.hour // 6) * 6, minute=0, second=0, microsecond=0)
        key = self.run_key_override or f"demo:{cycle:%Y%m%d%H}"
        return RunInfo(
            run_key=key,
            issued_at=cycle,
            wave_model_run_at=cycle,
            atmosphere_model_run_at=cycle,
            metadata_source="demo",
        )

    def fetch(self, spots: list[SpotPoint], run: RunInfo) -> FetchResult:
        result = FetchResult()
        for p in spots:
            if p.params is None:
                result.errors[p.spot_id] = "spot parameters missing"
                continue
            gen = DemoSeriesGenerator(p.params, run.run_key)
            result.records[p.spot_id] = gen.generate(
                run.issued_at, self.forecast_days * 24, self.overrides
            )
        return result
