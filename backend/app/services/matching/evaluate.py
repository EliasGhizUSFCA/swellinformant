"""Evaluate one swell event against one saved search (pure logic over timesteps).

Events are detected with a deliberately permissive threshold; each search then extracts
its own best sub-window: the daylight hours that satisfy *its* quality, size, period,
wind, direction and consistency filters, clustered with the same rules as events.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import mean
from typing import Any

from app.models.enums import DestinationMode, QualityLabel, WindRequirement
from app.services.geo import in_window
from app.services.surf_quality.scoring import label_for_score, min_score_for_label
from app.services.swell_detection.clustering import Step, best_cluster, cluster_steps

GOOD_WIND = {"glassy", "offshore", "cross-offshore"}
BAD_WIND = {"onshore", "cross-onshore"}


@dataclass(frozen=True)
class SearchCriteria:
    wave_min_ft: float
    wave_max_ft: float
    min_score: int
    min_period_s: float | None = None
    max_wind_kmh: float | None = None
    wind_requirement: WindRequirement = WindRequirement.ANY
    swell_direction_min: float | None = None
    swell_direction_max: float | None = None
    min_consistency: int | None = None
    min_window_hours: int = 4

    @classmethod
    def from_search(cls, search: Any) -> SearchCriteria:
        return cls(
            wave_min_ft=search.wave_min_ft,
            wave_max_ft=search.wave_max_ft,
            min_score=min_score_for_label(search.min_quality),
            min_period_s=search.min_period_s,
            max_wind_kmh=search.max_wind_kmh,
            wind_requirement=WindRequirement(search.wind_requirement),
            swell_direction_min=search.swell_direction_min,
            swell_direction_max=search.swell_direction_max,
            min_consistency=search.min_consistency,
            min_window_hours=search.min_window_hours,
        )


@dataclass
class UserWindow:
    start: datetime
    end: datetime
    qualifying_hours: int
    peak_score: int
    avg_score: float
    peak_label: QualityLabel
    height_min_ft: float
    height_max_ft: float
    confidence: int
    peak_time: datetime


def step_meets(pred: Any, fc: Any, c: SearchCriteria) -> bool:
    if pred.score < c.min_score:
        return False
    mid = (pred.breaking_height_min_ft + pred.breaking_height_max_ft) / 2.0
    if not (c.wave_min_ft <= mid <= c.wave_max_ft):
        return False
    period = fc.primary_swell_period_s or fc.wave_period_s
    if c.min_period_s is not None and (period is None or period < c.min_period_s):
        return False
    if c.max_wind_kmh is not None and (
        fc.wind_speed_kmh is None or fc.wind_speed_kmh > c.max_wind_kmh
    ):
        return False
    relation = pred.wind_relation
    if c.wind_requirement == WindRequirement.OFFSHORE and relation not in GOOD_WIND:
        return False
    if c.wind_requirement == WindRequirement.NOT_ONSHORE and relation in BAD_WIND:
        return False
    if c.swell_direction_min is not None and c.swell_direction_max is not None:
        direction = fc.primary_swell_direction_deg
        if direction is None or not in_window(
            direction, c.swell_direction_min, c.swell_direction_max
        ):
            return False
    if c.min_consistency is not None:
        consistency = (pred.components or {}).get("consistency")
        if consistency is None or consistency * 100 < c.min_consistency:
            return False
    return True


def evaluate_steps(
    steps: list[Step], c: SearchCriteria, break_daylight_hours: float
) -> UserWindow | None:
    clusters = cluster_steps(
        steps,
        qualifies=lambda s: step_meets(s.payload[0], s.payload[1], c),
        min_hours=c.min_window_hours,
        break_daylight_hours=break_daylight_hours,
    )
    best = best_cluster(clusters)
    if best is None:
        return None
    peak_pred = best.peak.payload[0]
    return UserWindow(
        start=best.start,
        end=best.end,
        qualifying_hours=int(round(best.qualifying_hours)),
        peak_score=peak_pred.score,
        avg_score=round(best.avg_score, 1),
        peak_label=label_for_score(peak_pred.score),
        height_min_ft=peak_pred.breaking_height_min_ft,
        height_max_ft=peak_pred.breaking_height_max_ft,
        confidence=int(round(mean(s.payload[0].confidence for s in best.steps))),
        peak_time=peak_pred.valid_time,
    )


def search_allows_spot(search: Any, spot: Any) -> bool:
    if not spot.is_active:
        return False
    if search.break_types and str(spot.break_type) not in set(search.break_types):
        return False
    mode = DestinationMode(search.destination_mode)
    if mode == DestinationMode.ALL:
        return True
    for d in search.destinations:
        if mode == DestinationMode.SPOTS and d.spot_id == spot.id:
            return True
        if mode == DestinationMode.REGIONS and (
            (d.region_group and d.region_group == spot.region_group)
            or (d.country_code and d.country_code == spot.country_code)
        ):
            return True
    return False
