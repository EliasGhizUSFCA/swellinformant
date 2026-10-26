"""Cluster-to-event matching of swell detection (pure, no database).

Events and clusters are made up for the test (transient SwellEvent objects, synthetic
steps); only their start and end times matter here. Events are listed by start time, as
detect_for_spot reads them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models import SwellEvent
from app.services.swell_detection.clustering import Cluster, Step
from app.services.swell_detection.service import MATCH_TOLERANCE, match_clusters

D0 = datetime(2026, 10, 21, tzinfo=UTC)


def at(hours: float) -> datetime:
    return D0 + timedelta(hours=hours)


def cluster(start: float, end: float) -> Cluster:
    """A cluster covering [start, end) hours after D0 (one synthetic step)."""
    return Cluster(
        steps=[Step(time=at(start), is_daylight=True, score=80, duration=at(end) - at(start))]
    )


def event(start: float, end: float) -> SwellEvent:
    return SwellEvent(start_time=at(start), end_time=at(end))


def test_two_close_events_each_keep_their_own_cluster() -> None:
    # 9 h apart, inside the 12 h tolerance: the first cluster also "overlaps" the second
    # event. The greedy matching gave the first cluster both events (cancelling the second)
    # and created the second again on every pass over the same data.
    first, second = event(6, 12), event(21, 27)
    assert at(21) - at(12) < MATCH_TOLERANCE
    matched, merges = match_clusters([cluster(6, 12), cluster(21, 27)], [first, second])
    assert matched == [0, 1]
    assert merges == {}


def test_a_cluster_spanning_two_events_keeps_the_earlier_and_merges_the_other() -> None:
    # The cluster overlaps the second event more (12 h against 6 h), but nothing else
    # continues either, so the swell first seen keeps its identity (as before) and the
    # second is cancelled into it.
    first, second = event(6, 12), event(21, 33)
    matched, merges = match_clusters([cluster(6, 33)], [first, second])
    assert matched == [0]
    assert merges == {1: 0}


def test_of_three_absorbed_events_the_earliest_continues() -> None:
    events = [event(0, 4), event(10, 14), event(20, 40)]  # overlaps 4, 4 and 20 h
    matched, merges = match_clusters([cluster(0, 40)], events)
    assert matched == [0]
    assert merges == {1: 0, 2: 0}


def test_an_event_split_in_two_continues_with_the_larger_part() -> None:
    whole = event(6, 30)
    matched, merges = match_clusters([cluster(6, 9), cluster(18, 30)], [whole])
    assert matched == [None, 0]  # the 3 h part becomes a new event
    assert merges == {}


def test_an_event_another_cluster_continues_is_never_merged() -> None:
    # The middle cluster touches both neighbours' events within the tolerance, but each of
    # them is continued by its own cluster, so nothing is cancelled.
    a, b, c = event(0, 6), event(15, 21), event(30, 36)
    clusters = [cluster(0, 6), cluster(15, 21), cluster(30, 36)]
    matched, merges = match_clusters(clusters, [a, b, c])
    assert matched == [0, 1, 2]
    assert merges == {}


def test_a_close_neighbour_is_never_taken_from_the_cluster_that_continues_it() -> None:
    # The first cluster grew to within 3 h of the second event; it still leaves that event
    # to the second cluster and nothing merges.
    a, b = event(0, 10), event(13, 20)
    matched, merges = match_clusters([cluster(0, 11), cluster(13, 20)], [a, b])
    assert matched == [0, 1]
    assert merges == {}


def test_events_beyond_the_tolerance_are_not_matched() -> None:
    far = event(0, 3)
    assert at(16) - at(3) > MATCH_TOLERANCE
    matched, merges = match_clusters([cluster(16, 20)], [far])
    assert matched == [None]
    assert merges == {}


def test_equal_overlaps_go_to_the_earlier_cluster() -> None:
    whole = event(0, 24)
    matched, _ = match_clusters([cluster(2, 6), cluster(14, 18)], [whole])
    assert matched == [0, None]


def test_equal_overlaps_keep_the_earlier_event() -> None:
    a, b = event(0, 6), event(20, 26)
    assert match_clusters([cluster(0, 26)], [a, b]) == ([0], {1: 0})  # 6 h with each
