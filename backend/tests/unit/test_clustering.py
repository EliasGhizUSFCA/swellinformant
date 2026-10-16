from datetime import UTC, datetime, timedelta

from app.services.swell_detection.clustering import Step, best_cluster, cluster_steps, overlaps

T0 = datetime(2026, 8, 10, 0, tzinfo=UTC)


def series(pattern: str, start: datetime = T0) -> list[Step]:
    """One char per hour: 'G' good daylight, 'b' bad daylight, '.' night."""
    out = []
    for i, ch in enumerate(pattern):
        out.append(
            Step(
                time=start + timedelta(hours=i),
                is_daylight=ch != ".",
                score=80 if ch == "G" else 20,
            )
        )
    return out


def q(step: Step) -> bool:
    return step.score >= 50


def test_contiguous_good_hours_form_one_event() -> None:
    clusters = cluster_steps(series("bbGGGGGbb"), qualifies=q, min_hours=3)
    assert len(clusters) == 1
    assert clusters[0].qualifying_hours == 5
    assert clusters[0].start == T0 + timedelta(hours=2)
    assert clusters[0].end == T0 + timedelta(hours=7)


def test_nights_do_not_split_a_multi_day_swell() -> None:
    day = "GGGGbbbbbbbb"  # good morning, poor afternoon (8 poor daylight hours)
    clusters = cluster_steps(
        series(day + "." * 12 + day + "." * 12 + day),
        qualifies=q,
        min_hours=3,
        break_daylight_hours=9,
    )
    assert len(clusters) == 1
    assert clusters[0].qualifying_hours == 12


def test_a_full_poor_day_splits_events() -> None:
    clusters = cluster_steps(
        series("GGGG" + "b" * 10 + "GGGG"), qualifies=q, min_hours=3, break_daylight_hours=9
    )
    assert len(clusters) == 2


def test_short_windows_are_discarded() -> None:
    assert cluster_steps(series("bbGGbb"), qualifies=q, min_hours=3) == []


def test_best_cluster_prefers_more_surf() -> None:
    clusters = cluster_steps(
        series("GGG" + "b" * 10 + "GGGGGG"), qualifies=q, min_hours=3, break_daylight_hours=9
    )
    best = best_cluster(clusters)
    assert best is not None and best.qualifying_hours == 6


def test_peak_and_average() -> None:
    steps = [Step(T0 + timedelta(hours=i), True, s) for i, s in enumerate([60, 90, 70])]
    cluster = cluster_steps(steps, qualifies=q, min_hours=1)[0]
    assert cluster.peak.score == 90
    assert cluster.avg_score == 220 / 3


def test_overlaps_with_tolerance() -> None:
    a0, a1 = T0, T0 + timedelta(hours=10)
    b0, b1 = T0 + timedelta(hours=15), T0 + timedelta(hours=20)
    assert not overlaps(a0, a1, b0, b1)
    assert overlaps(a0, a1, b0, b1, tolerance=timedelta(hours=6))


def test_hours_not_steps_with_mixed_spacing() -> None:
    """Hourly steps then 3-hourly steps (as the blend to f120, then beyond): qualifying and
    failing hours sum the steps' daylight hours, so two 3-hour steps are 6 hours, not 2."""
    hourly = [
        Step(T0 + timedelta(hours=i), True, 80, duration=timedelta(hours=1)) for i in range(2)
    ]
    three = [
        Step(
            T0 + timedelta(hours=2 + 3 * i),
            True,
            80,
            duration=timedelta(hours=3),
            daylight=timedelta(hours=3),
        )
        for i in range(2)
    ]
    (cluster,) = cluster_steps([*hourly, *three], qualifies=q, min_hours=7)
    assert cluster.qualifying_hours == 8
    assert cluster.end == T0 + timedelta(hours=5 + 3)  # the last step at 05:00 lasts 3 h


def test_a_step_counts_only_its_daylight_part() -> None:
    dusk = Step(T0, True, 80, duration=timedelta(hours=3), daylight=timedelta(hours=1.25))
    assert dusk.hours == 1.25
    assert cluster_steps([dusk], qualifies=q, min_hours=1.5) == []
    (cluster,) = cluster_steps([dusk], qualifies=q, min_hours=1)
    assert cluster.qualifying_hours == 1.25


def test_three_hour_poor_steps_break_an_event_after_nine_hours() -> None:
    def three(i: int, score: int) -> Step:
        return Step(T0 + timedelta(hours=3 * i), True, score, duration=timedelta(hours=3))

    poor = [three(i, 20) for i in range(1, 4)]  # 9 failing daylight hours in 3 steps
    steps = [three(0, 80), *poor, three(4, 80)]
    assert len(cluster_steps(steps, qualifies=q, min_hours=1, break_daylight_hours=9)) == 2
    assert len(cluster_steps(steps[:3] + steps[4:], qualifies=q, min_hours=1)) == 1
