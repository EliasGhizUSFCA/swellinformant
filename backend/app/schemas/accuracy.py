"""Developer accuracy dashboard: verification metrics, pairs and the model registry."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class StationSummary(BaseModel):
    code: str
    name: str
    provider: str
    latitude: float
    longitude: float
    water_depth_m: float | None
    paired_spots: list[str]
    observations: int
    latest_observation: datetime | None
    pairs: int


class ModelVersionSummary(BaseModel):
    id: int
    label: str
    name: str
    version: int
    status: str
    model_type: str
    target: str
    created_at: datetime
    activated_at: datetime | None
    retired_at: datetime | None
    experiment_id: int | None
    # Training data identity: period, stations, sources, rows, dataset fingerprint if recorded.
    training: dict[str, Any]
    dataset_version: str | None
    # Each deployment criterion with its threshold, value and outcome.
    decision: dict[str, Any]
    applicability: dict[str, Any]
    test_metrics: dict[str, Any]


class ExperimentSummary(BaseModel):
    id: int
    name: str
    target: str
    status: str
    created_at: datetime
    finished_at: datetime | None
    conclusion: str


class PairCount(BaseModel):
    source_code: str
    model_version: str
    variable: str
    n: int
    first_valid: datetime
    last_valid: datetime


class AccuracyOverview(BaseModel):
    latest_validation_at: datetime | None
    latest_metrics_at: datetime | None
    pair_counts: list[PairCount]
    stations: list[StationSummary]
    models: list[ModelVersionSummary]
    experiments: list[ExperimentSummary]
    lead_buckets_h: list[int]
    notes: list[str]


class MetricRow(BaseModel):
    scope: str
    scope_key: str
    source_code: str
    model_version: str
    variable: str
    lead_bucket_h: int
    period_start: datetime
    period_end: datetime
    n: int
    bias: float | None
    mae: float | None
    rmse: float | None
    scatter_index: float | None
    hh: float | None
    correlation: float | None
    skill_vs_persistence: float | None
    skill_vs_climatology: float | None
    extra: dict[str, Any]
    computed_at: datetime


class ScatterPoint(BaseModel):
    station: str
    valid_time: datetime
    lead_hours: float
    forecast: float
    observed: float
    error: float


class HistogramBin(BaseModel):
    lo: float
    hi: float
    count: int


class DailyStat(BaseModel):
    day: datetime
    n: int
    bias: float
    mae: float
    rmse: float


class PairsOut(BaseModel):
    variable: str
    unit: str
    source_code: str | None
    model_version: str
    n_total: int
    # A random but reproducible sample of at most ``limit`` pairs, for the scatter plot.
    sample: list[ScatterPoint]
    histogram: list[HistogramBin]
    daily: list[DailyStat]
