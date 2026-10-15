"""Partition map fields: the mixing guard, the joint mask, the sink and its byte budget.

All grids here are SYNTHETIC test patterns (constant boxes, halves, single cells), not
model output. The guard's values (resultant 0.8, present fraction 0.5) are heuristic; see
app.services.forecasts.fields and docs/map-layers.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import pytest

from app.services.forecasts.fields import (
    CORE_FIELD_VARIABLES,
    PARTITION_FIELD_VARIABLES,
    PARTITION_MIN_RESULTANT,
    PARTITION_PRESENT_FRACTION,
    PARTITION_SYSTEMS,
    FieldSink,
    TargetGrid,
    complete_times,
    joint_mask,
    partition_times,
    regrid_variable,
)

CYCLE = datetime(2026, 10, 10, 18, tzinfo=UTC)
T = TargetGrid(0.5)
NODE = T.node(10.0, 20.0)


@dataclass
class Src:
    """A synthetic regional source grid (GridLike) around the node (10N, 20E)."""

    values: np.ndarray
    lat0: float
    lon0: float
    dlat: float
    dlon: float


def box16(left: float, right: float) -> Src:
    """0.125 deg cells: exactly 4 x 4 equal-weight cells in the node's 0.5 deg box, the left
    two columns holding ``left`` and the right two ``right``."""
    values = np.empty((4, 4))
    values[:, :2], values[:, 2:] = left, right
    return Src(values, 10.1875, 19.8125, -0.125, 0.125)


def box9(cells: list[float | None]) -> Src:
    """1/6 deg cells: exactly 3 x 3 equal-weight cells in the node's box (None = absent)."""
    values = np.array([np.nan if c is None else c for c in cells], dtype=float).reshape(3, 3)
    return Src(values, 10.0 + 1 / 6, 20.0 - 1 / 6, -1 / 6, 1 / 6)


def at_node(grid: np.ndarray) -> float:
    return float(grid[NODE])


# --------------------------------------------------------------------------- mixing guard
def test_the_guard_values_are_the_documented_heuristics() -> None:
    assert (PARTITION_MIN_RESULTANT, PARTITION_PRESENT_FRACTION) == (0.8, 0.5)
    assert [p.system for p in PARTITION_SYSTEMS] == ["primary", "secondary", "windsea"]
    assert [v for p in PARTITION_SYSTEMS for v in p.variables] == list(PARTITION_FIELD_VARIABLES)


def test_two_systems_in_one_box_give_no_partition_direction() -> None:
    # Half the box from 116 deg, half from 199 deg (a partition swap between neighbouring
    # cells): the unit-vector mean 157.5 deg matches no wave, resultant cos(41.5) = 0.749.
    src = box16(116.0, 199.0)
    assert np.isnan(at_node(regrid_variable("swell1_dir", src, T)))
    assert np.isnan(at_node(regrid_variable("swell2_dir", src, T)))
    assert np.isnan(at_node(regrid_variable("windsea_dir", src, T)))
    # The core peak direction keeps today's rule (nodata only where the vectors cancel).
    assert at_node(regrid_variable("wave_dir", src, T)) == pytest.approx(157.5)
    # Only the node of the box is touched: every other node has no source cell.
    others = np.isnan(regrid_variable("wave_dir", src, T))
    others[NODE] = True
    assert others.all()


def test_one_system_keeps_its_direction() -> None:
    assert at_node(regrid_variable("swell1_dir", box16(225.0, 225.0), T)) == pytest.approx(225.0)
    # Close directions (one system, 20 deg of spread) pass: resultant cos(10) = 0.985.
    assert at_node(regrid_variable("swell1_dir", box16(215.0, 235.0), T)) == pytest.approx(225.0)
    # Across north: 350 and 10 average to 0 (never 180).
    assert at_node(regrid_variable("swell2_dir", box16(350.0, 10.0), T)) == pytest.approx(0.0)


def test_a_partition_present_in_less_than_half_the_box_is_nodata() -> None:
    one = box9([None, None, None, None, 4.0, None, None, None, None])
    assert np.isnan(at_node(regrid_variable("swell1_hs", one, T)))
    six = box9([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, None, None, None])
    assert at_node(regrid_variable("swell1_hs", six, T)) == pytest.approx(3.5)  # present cells
    assert at_node(regrid_variable("windsea_tp", six, T)) == pytest.approx(3.5)
    # The core height is a plain mean of the valid cells, however few.
    assert at_node(regrid_variable("hs", one, T)) == pytest.approx(4.0)


def test_the_box_area_counts_sea_cells_only() -> None:
    # A coastal box: the top row is land (no total wave height). Present at 3 of the 6 sea
    # cells is half the sea area, so the partition is kept; at 2 of 6 it is not.
    sea = np.ones((3, 3), dtype=bool)
    sea[0, :] = False
    half = box9([None, None, None, 1.0, 2.0, 3.0, None, None, None])
    assert at_node(regrid_variable("swell2_hs", half, T, area=sea)) == pytest.approx(2.0)
    assert np.isnan(at_node(regrid_variable("swell2_hs", half, T)))  # whole box: 3 of 9
    third = box9([None, None, None, 1.0, 2.0, None, None, None, None])
    assert np.isnan(at_node(regrid_variable("swell2_hs", third, T, area=sea)))
    with pytest.raises(ValueError, match="does not match"):
        regrid_variable("swell2_hs", half, T, area=np.ones((2, 2), dtype=bool))


def test_the_joint_mask_makes_a_partition_all_valid_or_all_nodata() -> None:
    hs = regrid_variable("swell1_hs", box16(1.5, 1.5), T)
    tp = regrid_variable("swell1_tp", box16(14.0, 14.0), T)
    mixed = regrid_variable("swell1_dir", box16(116.0, 199.0), T)  # nodata by the guard
    grids = {"swell1_hs": hs, "swell1_tp": tp, "swell1_dir": mixed}
    assert not np.isnan(at_node(hs)) and not np.isnan(at_node(tp))
    joint_mask(grids)
    assert all(np.isnan(at_node(g)) for g in grids.values())
    agree = {
        "swell1_hs": regrid_variable("swell1_hs", box16(1.5, 1.5), T),
        "swell1_tp": regrid_variable("swell1_tp", box16(14.0, 14.0), T),
        "swell1_dir": regrid_variable("swell1_dir", box16(225.0, 225.0), T),
    }
    joint_mask(agree)
    masks = [np.isnan(g) for g in agree.values()]
    assert all(np.array_equal(masks[0], m) for m in masks) and not masks[0][NODE]


# --------------------------------------------------------------------------- the sink
def gfs_like(fill: float) -> Src:
    return Src(np.full((721, 1440), fill), 90.0, 0.0, -0.25, 0.25)


def stage_system(sink: FieldSink, fhour: int, prefix: str, values: tuple[float, ...]) -> None:
    for suffix, value in zip(("hs", "tp", "dir"), values, strict=False):
        sink.stage(fhour, f"{prefix}_{suffix}", gfs_like(value))


def test_staged_partitions_are_stored_per_complete_system() -> None:
    sink = FieldSink(CYCLE, [0], 0.5, partitions=True)
    sink.put([sink.encode(0, v, gfs_like(1.0)) for v in CORE_FIELD_VARIABLES])
    stage_system(sink, 0, "swell1", (1.5, 15.0, 225.0))
    stage_system(sink, 0, "windsea", (0.6, 6.0, 45.0))
    stage_system(sink, 0, "swell2", (0.8, 11.0))  # direction never arrived
    sink.flush(0)
    names = [f.variable for f in sink.fields]
    assert names == [*CORE_FIELD_VARIABLES, "swell1_hs", "swell1_tp", "swell1_dir"] + [
        "windsea_hs",
        "windsea_tp",
        "windsea_dir",
    ]
    by_name = {f.variable: f.values() for f in sink.fields}
    assert np.nanmax(by_name["swell1_dir"]) == pytest.approx(225.0)  # degrees FROM, no +180
    assert complete_times(sink.fields) == [CYCLE]  # partitions never decide completeness
    assert partition_times(sink.fields) == []  # secondary swell is missing
    assert sink.warnings == [
        "f000 secondary swell not stored: swell2_dir missing",
        "f000 map partitions incomplete: missing secondary swell",
    ]


def test_partition_warnings_only_when_partitions_are_wanted() -> None:
    sink = FieldSink(CYCLE, [0], 0.5, partitions=False)
    sink.put([sink.encode(0, v, gfs_like(1.0)) for v in CORE_FIELD_VARIABLES])
    assert sink.warnings == [] and not sink.wants_partitions(0)
    wanted = FieldSink(CYCLE, [0, 3], 0.5, partitions=True)
    assert wanted.wants_partitions(0) and not wanted.wants_partitions(1)
    assert wanted.warnings[-2:] == [
        "f000 map partitions incomplete: missing primary swell, secondary swell, wind sea",
        "f003 map partitions incomplete: missing primary swell, secondary swell, wind sea",
    ]
    # Core lines come first (only the first 50 warnings are kept in a run's details).
    assert wanted.warnings[0].startswith("f000 map fields incomplete: missing hs")


def test_a_discarded_hour_stores_no_partition() -> None:
    sink = FieldSink(CYCLE, [0], 0.5, partitions=True)
    stage_system(sink, 0, "swell1", (1.5, 15.0, 225.0))
    sink.discard(0)
    sink.flush(0)
    assert sink.fields == []


def test_the_budget_stops_partitions_first_then_the_run() -> None:
    mb = 1_000_000
    sink = FieldSink(CYCLE, [0, 3, 6], 0.5, partitions=True, max_bytes=1000 * mb)
    sink.charge(100 * mb)  # an index file already read
    assert sink.reserve(0, 500 * mb) and sink.budget_used == 600 * mb
    assert sink.reserve(0, 300 * mb, partitions=True) and sink.budget_used == 900 * mb
    # Partitions no longer fit: they stop for the rest of the run, the core goes on.
    assert not sink.reserve(3, 200 * mb, partitions=True) and sink.partitions_stopped
    assert not sink.wants_partitions(6) and sink.stopped is None
    assert sink.reserve(3, 100 * mb) and sink.budget_used == 1000 * mb
    assert not sink.reserve(6, 1, partitions=True)  # stopped already: no second warning
    # The core no longer fits: the map fields stop, and nothing more is granted.
    assert not sink.reserve(6, 1) and sink.stopped is not None
    assert not sink.reserve(6, 0)
    warnings = [w for w in sink.warnings if "MAP_FIELD_MAX_BYTES_PER_RUN" in w]
    assert warnings == [
        "f003: partition layers stopped for the rest of the run; MAP_FIELD_MAX_BYTES_PER_RUN "
        "(1000.0 MB) would be exceeded: 900.0 MB used, 200.00 MB more needed",
        "f006: map fields stopped; MAP_FIELD_MAX_BYTES_PER_RUN (1000.0 MB) would be exceeded: "
        "1000.0 MB used, 0.00 MB more needed; later map hours are not read",
    ]


def test_without_a_budget_everything_is_granted() -> None:
    sink = FieldSink(CYCLE, [0], 0.5, partitions=True)
    assert sink.reserve(0, 10**12) and sink.reserve(0, 10**12, partitions=True)
    assert sink.stopped is None and not sink.partitions_stopped
