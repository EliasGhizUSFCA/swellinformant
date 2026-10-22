"""Model-run detection (app.services.sync.detect): the rules of docs/sync.md with fake
providers and a frozen clock. No database: every plan here is made with ``db=None`` from
``KnownRuns``, as the daemon's idle check does.

The runs are SYNTHETIC test inputs (run keys and cycles made up for the rules), not
provider data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings, get_settings
from app.services.forecasts.base import ModelRunRef, RunInfo
from app.services.http import RunNotServedError, TransientProviderError
from app.services.sync import detect
from app.services.sync.detect import Probe, apply_result, plan_sync, promote_pending
from app.services.sync.types import (
    KnownRuns,
    StageResult,
    SyncPlan,
    SyncResult,
    known_key,
)

C06 = datetime(2026, 10, 19, 6, tzinfo=UTC)
C12 = datetime(2026, 10, 19, 12, tzinfo=UTC)
C18 = datetime(2026, 10, 19, 18, tzinfo=UTC)
T0 = C12 + timedelta(hours=5, minutes=20)  # Open-Meteo's GFS waves of 12Z appear ~5.3 h on

GFS = "open_meteo_noaa"
ECMWF = "open_meteo_ecmwf"
MAP = detect.FIELDS_SOURCE_CODE
GEFS = detect.GEFS_SOURCE_CODE


def gfs_run(wave: datetime, atmos: datetime | None = None) -> RunInfo:
    """An Open-Meteo GFS-family run: waves and atmosphere can be on different cycles."""
    atmos = atmos or wave
    return RunInfo(
        run_key=f"gfswave016:{wave:%Y%m%d%H}|gfs013:{atmos:%Y%m%d%H}",
        issued_at=min(wave, atmos),
        wave_model_run_at=wave,
        atmosphere_model_run_at=atmos,
        metadata_source="open_meteo_meta",
        model_runs=(ModelRunRef("ncep_gfswave016", wave), ModelRunRef("ncep_gfs013", atmos)),
    )


def single_run(prefix: str, cycle: datetime) -> RunInfo:
    return RunInfo(f"{prefix}:{cycle:%Y%m%d%H}", cycle, cycle, cycle, "noaa-s3-idx")


def map_run(cycle: datetime) -> RunInfo:
    return single_run("noaa-gfs-fields", cycle)


def ecmwf_run(cycle: datetime) -> RunInfo:
    return RunInfo(
        run_key=f"ecmwf_wam025:{cycle:%Y%m%d%H}|ecmwf_ifs025:{cycle:%Y%m%d%H}",
        issued_at=cycle,
        model_runs=(ModelRunRef("ecmwf_wam025", cycle), ModelRunRef("ecmwf_ifs025", cycle)),
    )


@dataclass
class World:
    """What each fake provider serves (a RunInfo, or an exception to raise)."""

    runs: dict[str, RunInfo | Exception] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def reader(self, code: str) -> Callable[[datetime], RunInfo]:
        def current_run(now: datetime) -> RunInfo:
            self.calls.append(code)
            value = self.runs[code]
            if isinstance(value, Exception):
                raise value
            return value

        return current_run


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()

    def probes(settings: Settings) -> list[Probe]:
        out = [
            Probe("spots", GFS, "open_meteo", current_run=w.reader(GFS), family="ncep_gfs"),
            Probe("spots", ECMWF, "open_meteo_ecmwf", w.reader(ECMWF), family="ecmwf_ifs"),
        ]
        if GEFS in w.runs:
            out.append(Probe("ensemble", GEFS, "gefs", current_run=w.reader(GEFS)))
        else:
            out.append(Probe("ensemble", GEFS, "gefs", disabled="GEFS_ENABLED=false"))
        out.append(Probe("map", MAP, "map_fields", current_run=w.reader(MAP)))
        return out

    monkeypatch.setattr(detect, "build_probes", probes)
    return w


def settings(**update: object) -> Settings:
    base = {
        "sync_coalesce_minutes": 90,
        "sync_retry_minutes": 15,
        "sync_max_attempts": 6,
        "sync_trigger_sources": "spots,map",
        "map_field_cycles": "00,12",
    }
    return get_settings().model_copy(update={**base, **update})


def known(**stored: str) -> KnownRuns:
    """KnownRuns with these stored keys (keyword = source code with '-' for '_')."""
    return KnownRuns(
        stored={k: v for k, v in stored.items()},
        map_cycle=None,
        first_seen={},
        attempts={},
        loaded_at=C06,
    )


def state(plan: SyncPlan, code: str) -> str:
    return next(c.state for c in plan.components if c.source_code == code)


def comp(plan: SyncPlan, code: str):  # type: ignore[no-untyped-def]
    return next(c for c in plan.components if c.source_code == code)


def stored_06() -> KnownRuns:
    return KnownRuns(
        stored={
            GFS: gfs_run(C06).run_key,
            ECMWF: ecmwf_run(C06 - timedelta(hours=6)).run_key,
            MAP: map_run(C06 - timedelta(hours=6)).run_key,
        },
        map_cycle=C06 - timedelta(hours=6),
        first_seen={},
        attempts={},
        loaded_at=C06,
    )


def everything_current(world: World) -> None:
    world.runs.update(
        {
            GFS: gfs_run(C06),
            ECMWF: ecmwf_run(C06 - timedelta(hours=6)),
            MAP: map_run(C06 - timedelta(hours=6)),
        }
    )


# --------------------------------------------------------------------------- the rules
def test_nothing_new_is_not_due(world: World) -> None:
    everything_current(world)
    plan = plan_sync(None, settings=settings(), now=T0, known=stored_06())
    assert not plan.due and plan.due_kinds() == frozenset()
    assert {c.state for c in plan.components} == {"current", "disabled"}
    assert plan.reasons == ("every component is current",)
    assert sorted(world.calls) == sorted([GFS, ECMWF, MAP])  # metadata only, one call each


def test_a_new_gfs_run_waits_for_the_map_then_syncs_the_spots_alone(world: World) -> None:
    everything_current(world)
    world.runs[GFS] = gfs_run(C12)  # the map of 12Z is not out yet
    k = stored_06()
    first = plan_sync(None, settings=settings(), now=T0, known=k)
    assert not first.due
    spot = comp(first, GFS)
    assert spot.state == "waiting" and spot.first_seen_at == T0
    assert f"{MAP} 2026-10-19 12Z" in (spot.detail or "")
    assert first.waiting_until == T0 + timedelta(minutes=90)
    assert k.first_seen == {known_key(GFS, gfs_run(C12).run_key): T0}

    still = plan_sync(None, settings=settings(), now=T0 + timedelta(minutes=89), known=k)
    assert not still.due and state(still, GFS) == "waiting"

    later = plan_sync(None, settings=settings(), now=T0 + timedelta(minutes=90), known=k)
    assert later.due and later.due_kinds() == frozenset({"spots"})
    assert state(later, GFS) == "new" and state(later, MAP) == "current"
    assert comp(later, GFS).first_seen_at == T0  # the first sighting is kept
    assert later.anchor_cycle == C12


def test_spots_and_map_of_one_cycle_make_one_due_plan(world: World) -> None:
    everything_current(world)
    world.runs[MAP] = map_run(C12)
    k = stored_06()
    early = plan_sync(None, settings=settings(), now=T0 - timedelta(minutes=30), known=k)
    assert not early.due and state(early, MAP) == "waiting"  # the map waits for the spots

    world.runs[GFS] = gfs_run(C12)
    plan = plan_sync(None, settings=settings(), now=T0, known=k)
    assert plan.due and plan.anchor_cycle == C12
    assert plan.due_kinds() == frozenset({"spots", "map"})
    assert state(plan, GFS) == state(plan, MAP) == "new"
    assert state(plan, ECMWF) == "current"


def test_the_coalescing_window_starts_at_the_first_sighting_of_either(world: World) -> None:
    everything_current(world)
    world.runs[MAP] = map_run(C12)
    k = stored_06()
    plan_sync(None, settings=settings(), now=T0, known=k)  # the map seen at T0
    world.runs[GFS] = gfs_run(C12, atmos=C06)  # waves of 12Z, GFS atmosphere still 06Z
    plan = plan_sync(None, settings=settings(), now=T0 + timedelta(minutes=30), known=k)
    assert not plan.due
    assert state(plan, GFS) == state(plan, MAP) == "waiting"
    assert "model domains still on an older cycle" in (comp(plan, GFS).detail or "")
    assert plan.waiting_until == T0 + timedelta(minutes=90)  # both wait until the same time
    late = plan_sync(None, settings=settings(), now=T0 + timedelta(minutes=90), known=k)
    assert late.due and late.due_kinds() == frozenset({"spots", "map"})


def test_a_run_whose_domains_disagree_waits_and_keeps_its_first_sighting(world: World) -> None:
    everything_current(world)
    k = stored_06()
    world.runs[GFS] = gfs_run(C18, atmos=C12)  # 18Z is not a map cycle: no map wait
    first = plan_sync(None, settings=settings(), now=T0, known=k)
    assert not first.due and state(first, GFS) == "waiting"
    # The atmosphere moves on before the waves' window ends: the window is not restarted.
    world.runs[GFS] = gfs_run(C18, atmos=C18)
    complete = plan_sync(None, settings=settings(), now=T0 + timedelta(minutes=5), known=k)
    assert complete.due and state(complete, GFS) == "new"
    assert comp(complete, GFS).first_seen_at == T0
    assert list(k.first_seen) == [known_key(GFS, gfs_run(C18).run_key)]
    assert complete.anchor_cycle is None  # 18Z is not a map cycle


def test_a_new_ecmwf_run_is_due_at_once(world: World) -> None:
    everything_current(world)
    world.runs[ECMWF] = ecmwf_run(C12)
    plan = plan_sync(None, settings=settings(), now=T0, known=stored_06())
    assert plan.due and plan.due_kinds() == frozenset({"spots"})
    assert [c.source_code for c in plan.components if c.state == "new"] == [ECMWF]
    assert plan.anchor_cycle is None


def test_ecmwf_does_not_wait_for_a_waiting_gfs_run(world: World) -> None:
    everything_current(world)
    world.runs[GFS] = gfs_run(C12)  # waits for the map
    world.runs[ECMWF] = ecmwf_run(C12)
    plan = plan_sync(None, settings=settings(), now=T0, known=stored_06())
    assert plan.due
    assert state(plan, ECMWF) == "new" and state(plan, GFS) == "waiting"


def test_a_new_ensemble_alone_is_not_due_but_is_taken_by_the_next_sync(world: World) -> None:
    everything_current(world)
    world.runs[GEFS] = single_run("gefs-wave", C12)
    k = stored_06()
    k.stored[GEFS] = single_run("gefs-wave", C06).run_key
    plan = plan_sync(None, settings=settings(), now=T0, known=k)
    assert not plan.due and state(plan, GEFS) == "new"
    assert plan.due_kinds() == frozenset({"ensemble"})
    assert any("(not a trigger)" in r for r in plan.reasons)
    # With "ensemble" among the triggers it is due by itself.
    trig = plan_sync(None, settings=settings(sync_trigger_sources="ensemble"), now=T0, known=k)
    assert trig.due


def test_six_failed_attempts_wait_for_the_next_run(world: World) -> None:
    everything_current(world)
    world.runs[ECMWF] = ecmwf_run(C12)
    k = stored_06()
    key = known_key(ECMWF, ecmwf_run(C12).run_key)
    k.attempts[key] = (6, T0 - timedelta(hours=2))
    plan = plan_sync(None, settings=settings(), now=T0, known=k)
    assert not plan.due and state(plan, ECMWF) == "failed"
    assert comp(plan, ECMWF).attempts == 6
    assert "6 failed attempts" in (comp(plan, ECMWF).detail or "")
    # The next run key is new again, and the old key's attempts are forgotten.
    world.runs[ECMWF] = ecmwf_run(C18)
    nxt = plan_sync(None, settings=settings(), now=T0, known=k)
    assert nxt.due and state(nxt, ECMWF) == "new"
    assert key not in k.attempts


def test_a_failed_attempt_is_retried_after_the_retry_interval(world: World) -> None:
    everything_current(world)
    world.runs[ECMWF] = ecmwf_run(C12)
    k = stored_06()
    k.attempts[known_key(ECMWF, ecmwf_run(C12).run_key)] = (1, T0)
    soon = plan_sync(None, settings=settings(), now=T0 + timedelta(minutes=14), known=k)
    assert not soon.due and state(soon, ECMWF) == "waiting"
    assert comp(soon, ECMWF).retry_after_s == pytest.approx(60.0)
    assert soon.waiting_until == T0 + timedelta(minutes=15)
    due = plan_sync(None, settings=settings(), now=T0 + timedelta(minutes=15), known=k)
    assert due.due and state(due, ECMWF) == "new"


def _failed_sync(code: str, run_key: str, retry_after_s: float | None) -> SyncResult:
    plan = SyncPlan(checked_at=T0, due=True, components=(), anchor_cycle=None, reasons=())
    stage = StageResult(
        "spots",
        "failed",
        T0,
        10,
        0.1,
        {
            "components": [
                {
                    "source_code": code,
                    "run_key": run_key,
                    "outcome": "failed",
                    "retry_after_s": retry_after_s,
                }
            ]
        },
    )
    return SyncResult(1, "failed", plan, [stage], None)


def test_the_providers_retry_after_is_honoured(world: World) -> None:
    everything_current(world)
    world.runs[ECMWF] = ecmwf_run(C12)
    k = stored_06()
    key = ecmwf_run(C12).run_key
    apply_result(k, _failed_sync(ECMWF, key, retry_after_s=3600), now=T0)
    assert k.attempts[known_key(ECMWF, key)][0] == 1
    s = settings()
    at_30 = plan_sync(None, settings=s, now=T0 + timedelta(minutes=30), known=k)
    assert not at_30.due and state(at_30, ECMWF) == "waiting"
    assert plan_sync(None, settings=s, now=T0 + timedelta(minutes=60), known=k).due
    # Without a provider hint the retry interval applies.
    k2 = stored_06()
    apply_result(k2, _failed_sync(ECMWF, key, retry_after_s=None), now=T0)
    assert plan_sync(None, settings=s, now=T0 + timedelta(minutes=15), known=k2).due


def test_attempts_count_up_and_a_stored_run_clears_them(world: World) -> None:
    k = stored_06()
    key = ecmwf_run(C12).run_key
    for _ in range(3):
        apply_result(k, _failed_sync(ECMWF, key, None), now=T0)
    assert k.attempts[known_key(ECMWF, key)][0] == 3
    ok = _failed_sync(ECMWF, key, None)
    ok.stages[0].detail["components"][0]["outcome"] = "stored"
    k.first_seen[known_key(ECMWF, key)] = T0
    apply_result(k, ok, now=T0)
    assert k.attempts == {} and k.first_seen == {}


def test_a_metadata_outage_is_unknown_and_not_due(world: World) -> None:
    everything_current(world)
    world.runs[GFS] = TransientProviderError(
        "Open-Meteo metadata returned HTTP 503 for https://x.example/meta.json?apikey=SECRET1"
    )
    world.runs[MAP] = RuntimeError("boom")
    plan = plan_sync(None, settings=settings(), now=T0, known=stored_06())
    assert not plan.due
    gfs = comp(plan, GFS)
    assert gfs.state == "unknown" and "HTTP 503" in (gfs.detail or "")
    assert "SECRET1" not in (gfs.detail or "") and "[REDACTED]" in (gfs.detail or "")
    assert comp(plan, MAP).detail == "RuntimeError: boom"
    assert any(r.startswith(f"{GFS}: ") for r in plan.reasons)


def test_a_run_the_provider_does_not_serve_unmixed_waits_without_an_attempt(
    world: World,
) -> None:
    everything_current(world)
    # Open-Meteo serves ECMWF 06Z between the 00Z and 12Z runs the family reads.
    world.runs[ECMWF] = RunNotServedError(
        "Open-Meteo now serves ecmwf_wam025 run 2026-10-19 06Z, which this source does not "
        "read (only 00/12Z runs)"
    )
    k = stored_06()
    plan = plan_sync(None, settings=settings(), now=T0, known=k)
    assert not plan.due and state(plan, ECMWF) == "waiting"
    assert (comp(plan, ECMWF).detail or "").startswith("not served yet: Open-Meteo now serves")
    assert plan.waiting_until is None and k.attempts == {} and k.first_seen == {}
    # While a newer run is being published Open-Meteo says when to look again.
    world.runs[ECMWF] = RunNotServedError("publishing ecmwf_ifs025 12Z", retry_after_s=600.0)
    soon = plan_sync(None, settings=settings(), now=T0, known=k)
    assert not soon.due and comp(soon, ECMWF).retry_after_s == 600.0
    assert soon.waiting_until == T0 + timedelta(seconds=600)
    # `sync run` cannot acquire what is not served: it stays waiting, nothing is due.
    now = promote_pending(soon)
    assert not now.due and state(now, ECMWF) == "waiting" and now.due_kinds() == frozenset()


class FakeOpenMeteo:
    """The calls detection makes on an Open-Meteo family provider (SYNTHETIC metadata)."""

    def __init__(self, served: bool) -> None:
        self.served = served
        self.meta_reads = 0

    def read_metas(self) -> dict[str, str]:
        self.meta_reads += 1
        return {"ecmwf_wam025": "meta"}

    def current_run(self, now: datetime) -> RunInfo:
        assert self.read_metas() == {"ecmwf_wam025": "meta"}
        return ecmwf_run(C06 - timedelta(hours=6))

    def ensure_servable(self, run: RunInfo, now: datetime | None = None) -> None:
        self.read_metas()
        if not self.served:
            raise RunNotServedError("can no longer be read unmixed")


def test_servability_is_checked_on_one_metadata_read() -> None:
    ok = FakeOpenMeteo(served=True)
    run = detect.servable_run(ok)(T0)
    assert run == ecmwf_run(C06 - timedelta(hours=6))
    assert ok.meta_reads == 1  # current_run and ensure_servable shared the read
    ok.read_metas()
    assert ok.meta_reads == 2  # the method is restored after the check
    refused = FakeOpenMeteo(served=False)
    with pytest.raises(RunNotServedError):
        detect.servable_run(refused)(T0)
    assert refused.meta_reads == 1 and "read_metas" not in vars(refused)


def test_force_is_due_and_takes_every_configured_component(world: World) -> None:
    everything_current(world)
    plan = plan_sync(None, settings=settings(), now=T0, known=stored_06(), force=True)
    assert plan.due and plan.forced and "forced" in plan.reasons
    assert plan.due_kinds() == frozenset({"spots", "map"})  # the ensemble is disabled
    assert state(plan, GFS) == "current"  # the states stay honest


def test_an_older_run_than_stored_is_never_new(world: World) -> None:
    everything_current(world)
    k = stored_06()
    k.stored[GFS] = gfs_run(C12).run_key
    world.runs[GFS] = gfs_run(C06)  # e.g. a metadata cache serving the previous run
    plan = plan_sync(None, settings=settings(), now=T0, known=k)
    assert not plan.due and state(plan, GFS) == "current"
    assert "older than the stored run" in (comp(plan, GFS).detail or "")


def test_a_run_with_a_model_domain_behind_the_stored_run_is_never_new(world: World) -> None:
    """A lagging metadata server offers the stored run with one domain still on the previous
    cycle: same newest cycle, different key, but nothing newer to fetch."""
    everything_current(world)
    k = stored_06()
    k.stored[GFS] = gfs_run(C12).run_key
    world.runs[GFS] = gfs_run(C06, atmos=C12)  # waves still on 06Z, atmosphere on 12Z
    plan = plan_sync(None, settings=settings(), now=T0, known=k)
    assert not plan.due and state(plan, GFS) == "current"
    assert "older than the stored run" in (comp(plan, GFS).detail or "")
    # The reverse (one domain ahead of the stored run) is a newer run.
    k.stored[GFS] = gfs_run(C06, atmos=C12).run_key
    world.runs[GFS] = gfs_run(C12)
    plan = plan_sync(None, settings=settings(), now=T0, known=k)
    assert (
        state(plan, GFS) != "current" and comp(plan, GFS).available_run_key == gfs_run(C12).run_key
    )


@pytest.mark.parametrize(
    ("available", "stored", "behind"),
    [
        ("w:2026101906|a:2026101912", "w:2026101912|a:2026101912", True),
        ("w:2026101912|a:2026101912", "w:2026101906|a:2026101912", False),  # ahead
        ("w:2026101912|a:2026101906", "w:2026101906|a:2026101912", False),  # mixed
        ("w:2026101906|a:2026101912", "w:2026101906|a:2026101912", False),  # the same
        ("w:2026101906|b:2026101912", "w:2026101912|a:2026101912", False),  # other domains
        ("demo:2026101906", "demo:2026101912", True),
        ("demo-sim:20261019143012123456", "demo:2026101912", False),
        (None, "w:2026101912", False),
    ],
)
def test_domains_behind_compares_domain_by_domain(
    available: str | None, stored: str | None, behind: bool
) -> None:
    assert detect.domains_behind(available, stored) is behind


def test_the_map_is_current_when_its_cycle_is_published(world: World) -> None:
    everything_current(world)
    k = stored_06()
    k.stored.pop(MAP)
    k.map_cycle = C12  # e.g. the map layers stored by another source of the same cycle
    world.runs[MAP] = map_run(C12)
    plan = plan_sync(None, settings=settings(), now=T0, known=k)
    assert state(plan, MAP) == "current" and not plan.due


def test_nothing_known_means_everything_is_new(world: World) -> None:
    everything_current(world)
    plan = plan_sync(None, settings=settings(), now=T0)
    assert plan.due and {state(plan, c) for c in (GFS, ECMWF, MAP)} == {"new"}


def test_without_coalescing_a_gfs_run_is_due_at_once(world: World) -> None:
    everything_current(world)
    world.runs[GFS] = gfs_run(C12)
    plan = plan_sync(None, settings=settings(sync_coalesce_minutes=0), now=T0, known=stored_06())
    assert plan.due and state(plan, GFS) == "new"


def test_promote_pending_runs_held_components_now(world: World) -> None:
    everything_current(world)
    world.runs[GFS] = gfs_run(C12)  # waiting for the map
    plan = plan_sync(None, settings=settings(), now=T0, known=stored_06())
    assert not plan.due
    now = promote_pending(plan)
    assert now.due and state(now, GFS) == "new" and now.due_kinds() == frozenset({"spots"})
    assert state(now, MAP) == "current"  # nothing newer to fetch there
    assert now.waiting_until is None


def test_the_plan_is_json_safe(world: World) -> None:
    import json

    everything_current(world)
    world.runs[GFS] = gfs_run(C12)
    plan = plan_sync(None, settings=settings(), now=T0, known=stored_06())
    data = json.loads(json.dumps(plan.as_dict()))
    assert data["due"] is False and data["waiting_until"] == "2026-10-19T18:50:00+00:00"


# --------------------------------------------------------------------------- helpers
@pytest.mark.parametrize(
    ("key", "cycle"),
    [
        ("gfswave016:2026101912|gfs013:2026101906", datetime(2026, 10, 19, 12, tzinfo=UTC)),
        ("noaa-gfs-fields:2026101900", datetime(2026, 10, 19, 0, tzinfo=UTC)),
        ("demo:2026101918", datetime(2026, 10, 19, 18, tzinfo=UTC)),
        ("demo-sim:20261019143012123456", None),
        ("gefs-wave:2026131900", None),  # not a date
        (None, None),
    ],
)
def test_newest_cycle_reads_run_keys(key: str | None, cycle: datetime | None) -> None:
    assert detect.newest_cycle(key) == cycle


def test_build_probes_follows_the_settings() -> None:
    demo = detect.build_probes(get_settings().model_copy(update={"forecast_providers": "demo"}))
    assert [(p.kind, p.source_code, p.disabled is None) for p in demo] == [
        ("spots", "demo", True),
        ("ensemble", GEFS, False),
        ("map", MAP, False),
    ]
    live = detect.build_probes(
        get_settings().model_copy(
            update={
                "forecast_providers": "open_meteo,open_meteo_ecmwf",
                "map_fields_enabled": True,
                "gefs_enabled": True,
            }
        )
    )
    try:
        assert [(p.kind, p.source_code, p.provider, p.family) for p in live] == [
            ("spots", GFS, "open_meteo", "ncep_gfs"),
            ("spots", ECMWF, "open_meteo_ecmwf", "ecmwf_ifs"),
            ("ensemble", GEFS, "gefs", None),
            ("map", MAP, "map_fields", None),
        ]
        assert all(p.current_run is not None and p.error is None for p in live)
        # The checks use a short HTTP timeout (a hanging host must not hold the daemon).
        clients = [p.close.__self__ for p in live if p.close is not None]  # type: ignore[attr-defined]
        assert len(clients) == 4
        assert {c.timeout.connect for c in clients} == {detect.PROBE_TIMEOUT_SECONDS}
    finally:
        for p in live:
            if p.close:
                p.close()
