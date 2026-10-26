"""Plain data containers for the scoring engine (decoupled from the ORM)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from app.services.geo import window_center
from app.services.surf_quality.nearshore import Profile
from app.services.surf_quality.wind import WindProfile, wind_profile_from_dict

# Record extra key naming the gust definition of a forecast hour (forecasts.base
# EXTRA_GUST_KIND; repeated here so the scoring engine does not import the provider layer).
EXTRA_GUST_KIND = "gust_kind"


@dataclass(frozen=True)
class SpotParams:
    slug: str
    latitude: float
    longitude: float
    swell_window_min: float
    swell_window_max: float
    optimal_swell_direction_min: float
    optimal_swell_direction_max: float
    offshore_wind_direction: float
    min_swell_period_s: float
    ideal_swell_period_s: float
    wave_height_min_ft: float
    wave_height_max_ft: float
    height_factor: float = 1.0
    tide_preference: str | None = None
    calibration: dict[str, Any] | None = None
    # Physical description for the nearshore model; None = derived from the fields above.
    profile: Profile | None = None
    # Researched wind tolerance and shelter (wind.py); None = the heuristic defaults.
    wind_profile: WindProfile | None = None
    # Tide research confidence ("high" | "moderate" | "low", None = not assessed) and the
    # profile decision on the tide preference ("changed" | "kept" | "disputed" | None). The
    # tide is scored only when the class is moderate or high, the decision is not disputed
    # and the spot has a preference (scoring.tide_excluded_reason).
    tide_research_class: str | None = None
    tide_decision: str | None = None

    def physical_profile(self) -> Profile:
        """The stored profile, or one derived from the spot's windows (all estimated)."""
        if self.profile is not None:
            return self.profile
        return Profile(
            shore_normal_deg=window_center(
                self.optimal_swell_direction_min, self.optimal_swell_direction_max
            ),
            swell_window_min=self.swell_window_min,
            swell_window_max=self.swell_window_max,
            focusing_ref=self.height_factor,
            data_quality={"shore_normal_deg": "estimated", "focusing_ref": "estimated"},
        )

    @classmethod
    def from_orm(cls, spot: Any) -> SpotParams:
        return cls(
            slug=spot.slug,
            latitude=spot.latitude,
            longitude=spot.longitude,
            swell_window_min=spot.swell_window_min,
            swell_window_max=spot.swell_window_max,
            optimal_swell_direction_min=spot.optimal_swell_direction_min,
            optimal_swell_direction_max=spot.optimal_swell_direction_max,
            offshore_wind_direction=spot.offshore_wind_direction,
            min_swell_period_s=spot.min_swell_period_s,
            ideal_swell_period_s=spot.ideal_swell_period_s,
            wave_height_min_ft=spot.wave_height_min_ft,
            wave_height_max_ft=spot.wave_height_max_ft,
            height_factor=spot.height_factor,
            tide_preference=str(spot.tide_preference) if spot.tide_preference else None,
            calibration=spot.calibration,
            profile=profile_from_orm(spot),
            wind_profile=wind_profile_from_orm(spot),
        )


def profile_from_orm(spot: Any) -> Profile | None:
    row = getattr(spot, "physical_profile", None)
    if row is None:
        return None
    return Profile(
        shore_normal_deg=row.shore_normal_deg,
        swell_window_min=spot.swell_window_min,
        swell_window_max=spot.swell_window_max,
        forecast_point_depth_m=row.forecast_point_depth_m,
        controlling_depth_m=row.controlling_depth_m,
        breaker_index=row.breaker_index,
        focusing_ref=row.focusing_ref,
        focusing_kappa=row.focusing_kappa,
        focusing_t_star_s=row.focusing_t_star_s,
        shadow_table=tuple(row.shadow_table) if row.shadow_table else None,
        spreading_deg=row.spreading_deg,
        data_quality=dict(row.data_quality or {}),
    )


def wind_profile_from_orm(spot: Any) -> WindProfile | None:
    row = getattr(spot, "physical_profile", None)
    return wind_profile_from_dict(getattr(row, "wind_profile", None)) if row is not None else None


# Offshore wave heights of a sea state (an Hs correction scales all of them alike).
HEIGHT_FIELDS = (
    "sig_wave_height_m",
    "primary_swell_height_m",
    "secondary_swell_height_m",
    "tertiary_swell_height_m",
    "wind_wave_height_m",
)


@dataclass(frozen=True)
class SeaState:
    """Offshore conditions for one valid time (metres, seconds, degrees FROM, km/h)."""

    sig_wave_height_m: float | None = None
    wave_period_s: float | None = None
    wave_direction_deg: float | None = None
    primary_swell_height_m: float | None = None
    primary_swell_period_s: float | None = None
    primary_swell_direction_deg: float | None = None
    secondary_swell_height_m: float | None = None
    secondary_swell_period_s: float | None = None
    secondary_swell_direction_deg: float | None = None
    tertiary_swell_height_m: float | None = None
    tertiary_swell_period_s: float | None = None
    tertiary_swell_direction_deg: float | None = None
    wind_wave_height_m: float | None = None
    wind_wave_period_s: float | None = None
    wind_wave_direction_deg: float | None = None
    wind_speed_kmh: float | None = None
    wind_direction_deg: float | None = None
    wind_gust_kmh: float | None = None
    # The source's gust definition (schemas.spots.GustKind), when it recorded one.
    wind_gust_kind: str | None = None
    # 0 at the preceding low water, 1 at the next high water (tides.stage.TideStage).
    tide_stage: float | None = None

    def scaled(self, factor: float) -> SeaState:
        """The same sea state with every offshore height multiplied by ``factor``."""
        changes = {
            name: getattr(self, name) * factor
            for name in HEIGHT_FIELDS
            if getattr(self, name) is not None
        }
        return replace(self, **changes)

    @classmethod
    def from_orm(cls, row: Any, tide_stage: float | None = None) -> SeaState:
        return cls(
            sig_wave_height_m=row.sig_wave_height_m,
            wave_period_s=row.wave_period_s,
            wave_direction_deg=row.wave_direction_deg,
            primary_swell_height_m=row.primary_swell_height_m,
            primary_swell_period_s=row.primary_swell_period_s,
            primary_swell_direction_deg=row.primary_swell_direction_deg,
            secondary_swell_height_m=row.secondary_swell_height_m,
            secondary_swell_period_s=row.secondary_swell_period_s,
            secondary_swell_direction_deg=row.secondary_swell_direction_deg,
            tertiary_swell_height_m=getattr(row, "tertiary_swell_height_m", None),
            tertiary_swell_period_s=getattr(row, "tertiary_swell_period_s", None),
            tertiary_swell_direction_deg=getattr(row, "tertiary_swell_direction_deg", None),
            wind_wave_height_m=row.wind_wave_height_m,
            wind_wave_period_s=row.wind_wave_period_s,
            wind_wave_direction_deg=row.wind_wave_direction_deg,
            wind_speed_kmh=row.wind_speed_kmh,
            wind_direction_deg=row.wind_direction_deg,
            wind_gust_kmh=row.wind_gust_kmh,
            wind_gust_kind=_gust_kind(row),
            tide_stage=tide_stage,
        )


def _gust_kind(row: Any) -> str | None:
    kind = (getattr(row, "extra", None) or {}).get(EXTRA_GUST_KIND)
    return str(kind) if kind else None


@dataclass(frozen=True)
class PartitionFlux:
    """One partition's predicted breaking height (unrounded), for the partition-weighted
    direction and period fits and the consistency term."""

    name: str  # primary_swell | secondary_swell | tertiary_swell | wind_sea | combined
    height_m: float
    period_s: float
    direction_deg: float | None
    breaking_m: float


@dataclass
class BreakingEstimate:
    """Breaking face heights in feet.

    ``mid_ft`` is the significant breaking height (typical larger waves); ``min_ft`` and
    ``max_ft`` are the reported typical-to-sets range. Forecast uncertainty (p10/p90) is a
    separate quantity computed in ``uncertainty.py``.
    """

    min_ft: float
    max_ft: float
    mid_ft: float
    method: str
    calibrated: bool
    partitions: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    depth_limited: bool = False
    # The same partitions unrounded (physical model only; empty for the calibrated model).
    flux: list[PartitionFlux] = field(default_factory=list)


@dataclass
class QualityResult:
    score: int
    label: str
    # Factor fits (0-1) and gates; the full stored contract is ``stored_components``.
    components: dict[str, float]
    breaking: BreakingEstimate
    wind_relation: str
    explanation: str
    # Algorithm 2.1.0 additions (docs/SURF_SCORING.md section 3a): wind_detail,
    # weights_used, contributions, direction_partitions and tide_detail or
    # tide_excluded_reason.
    details: dict[str, Any] = field(default_factory=dict)

    def stored_components(self) -> dict[str, Any]:
        """``components`` plus the 2.1.0 keys, as written to the prediction row."""
        return {**self.components, **self.details}
