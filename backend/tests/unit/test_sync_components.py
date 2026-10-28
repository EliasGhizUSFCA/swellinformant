"""Per-component detection (design D6, D9; app.services.sync.detect) with fake providers
and a frozen clock, no database: the plans are made with ``db=None`` from ``KnownRuns``, as
an idle check does.

The runs are SYNTHETIC test inputs: Open-Meteo-shaped run keys and cycles made up for the
rules (wave = the GFS-Wave domains, atmosphere = the GFS domains), not provider data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from app.core.config import Settings, get_settings
from app.services.forecasts.base import ModelRunRef, RunInfo
from app.services.http import RunNotServedError
from app.services.sync import detect
from app.services.sync.detect import PartlyServedRun, Probe, limit_plan, plan_sync
from app.services.sync.types import (
    SPOT_RETRY_BACKOFF_MINUTES,
    KnownRuns,
    SpotRetry,
    known_key,
    spot_retry_wait,
)

C00 = datetime(2026, 10, 27, 0, tzinfo=UTC)
C06 = datetime(2026, 10, 27, 6, tzinfo=UTC)
T0 = datetime(2026, 10, 27, 11, 31, tzinfo=UTC)
SRC = "open_meteo_noaa"


def wave_key(cycle: datetime) -> str:
    return f"gfswave016:{cycle:%Y%m%d%H}|gfswave025:{cycle:%Y%m%d%H}"


def atmos_key(cycle: datetime, cycle025: datetime | None = None) -> str:
    return f"gfs013:{cycle:%Y%m%d%H}|gfs025:{(cycle025 or cycle):%Y%m%d%H}"


def om_run(wave: datetime, atmos: datetime, atmos025: datetime | None = None) -> RunInfo:
    """An Open-Meteo GFS-family run whose components are on their own cycles."""
    keys = {"wave": wave_key(wave), "atmosphere": atmos_key(atmos, atmos025)}
    return RunInfo(
        run_key=f"{keys['wave']}|{keys['atmosphere']}",
        issued_at=min(wave, atmos, atmos025 or atmos),
        wave_model_run_at=wave,
        atmosphere_model_run_at=min(atmos, atmos025 or atmos),
        metadata_source="open_meteo_meta",
        model_runs=(
            ModelRunRef("ncep_gfswave016", wave, component="wave"),
            ModelRunRef("ncep_gfswave025", wave, component="wave"),
            ModelRunRef("ncep_gfs013", atmos, component="atmosphere"),
            ModelRunRef("ncep_gfs025", atmos025 or atmos, component="atmosphere"),
        ),
        component_keys=MappingProxyType(keys),
    )


@dataclass
class World:
    run: RunInfo | Exception = field(default_factory=lambda: om_run(C00, C00))
    calls: int = 0

    def reader(self) -> Callable[[datetime], RunInfo]:
        def current_run(now: datetime) -> RunInfo:
            self.calls += 1
            if isinstance(self.run, Exception):
                raise self.run
            return self.run

        return current_run


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()

    def probes(settings: Settings) -> list[Probe]:
        return [
            Probe("spots", SRC, "open_meteo", current_run=w.reader(), family="ncep_gfs"),
            Probe("ensemble", "noaa_gefs_wave", "gefs", disabled="GEFS_ENABLED=false"),
            Probe("map", "noaa_gfs_fields", "map_fields", disabled="map fields are off"),
        ]

    monkeypatch.setattr(detect, "build_probes", probes)
    return w


def settings(**update: object) -> Settings:
    base = {
        "forecast_providers": "open_meteo",
        "sync_coalesce_minutes": 0,
        "sync_retry_minutes": 15,
        "sync_max_attempts": 6,
        "sync_trigger_sources": "spots,map",
    }
    return get_settings().model_copy(update={**base, **update})


def stored(wave: datetime, atmos: datetime, **extra: object) -> KnownRuns:
    run = om_run(wave, atmos)
    return KnownRuns(
        stored={
            SRC: run.run_key,
            f"{SRC}:wave": run.component_keys["wave"],
            f"{SRC}:atmosphere": run.component_keys["atmosphere"],
        },
        map_cycle=None,
        first_seen={},
        attempts={},
        loaded_at=C00,
        **extra,  # type: ignore[arg-type]
    )


def parts(plan: object) -> dict[str | None, str]:
    return {c.component: c.state for c in plan.components if c.source_code == SRC}  # type: ignore[attr-defined]


def part(plan: object, component: str):  # type: ignore[no-untyped-def]
    return next(c for c in plan.components if c.component == component)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- detection
def test_a_new_wave_run_is_due_alone_and_the_atmosphere_stays_current(world: World) -> None:
    world.run = om_run(C06, C00)  # GFS-Wave 06Z published, GFS still 00Z (as at 11:31Z)
    k = stored(C00, C00)
    plan = plan_sync(None, settings=settings(), now=T0, known=k)
    assert plan.due and parts(plan) == {"wave": "new", "atmosphere": "current"}
    wave = part(plan, "wave")
    assert wave.available_run_key == wave_key(C06) and wave.stored_run_key == wave_key(C00)
    assert wave.available_cycle == C06 and wave.first_seen_at == T0
    assert part(plan, "atmosphere").available_run_key == atmos_key(C00)
    assert k.first_seen == {known_key(f"{SRC}:wave", wave_key(C06)): T0}
    assert world.calls == 1  # one metadata read; no data request
    assert any("wave: new run" in r for r in plan.reasons)


def test_a_new_atmosphere_run_is_due_alone(world: World) -> None:
    world.run = om_run(C00, C06)
    plan = plan_sync(None, settings=settings(), now=T0, known=stored(C00, C00))
    assert plan.due and parts(plan) == {"wave": "current", "atmosphere": "new"}


def test_an_unchanged_run_is_not_due(world: World) -> None:
    plan = plan_sync(None, settings=settings(), now=T0, known=stored(C00, C00))
    assert not plan.due and parts(plan) == {"wave": "current", "atmosphere": "current"}
    assert plan.reasons == ("every component is current",)


def test_a_component_older_than_stored_is_never_new(world: World) -> None:
    """Monotonic: a lagging server offering the previous wave run is current, not new."""
    world.run = om_run(C00, C06)
    plan = plan_sync(None, settings=settings(), now=T0, known=stored(C06, C06))
    assert not plan.due and parts(plan) == {"wave": "current", "atmosphere": "current"}
    assert "older than the stored run" in (part(plan, "wave").detail or "")


def legacy(wave: datetime, atmos: datetime) -> KnownRuns:
    """What Upgrade 4 stored: one whole-source run key, no component keys."""
    return KnownRuns(
        stored={SRC: om_run(wave, atmos).run_key},
        map_cycle=None,
        first_seen={},
        attempts={},
        loaded_at=C00,
    )


def test_a_run_stored_before_components_existed_is_split_by_model_domain(world: World) -> None:
    """The upgrade path: each component is compared with the part of the whole stored run
    key that names its own model domains. The waves (00Z, older than the stored wind of
    06Z) stay current while a new atmosphere run is due; never both new, never "older"."""
    C12 = C06 + timedelta(hours=6)
    world.run = om_run(C00, C12)
    plan = plan_sync(None, settings=settings(), now=T0, known=legacy(C00, C06))
    assert plan.due and parts(plan) == {"wave": "current", "atmosphere": "new"}
    assert part(plan, "wave").stored_run_key == wave_key(C00)
    assert part(plan, "atmosphere").stored_run_key == atmos_key(C06)
    world.run = om_run(C00, C06)  # nothing new since the upgrade
    plan = plan_sync(None, settings=settings(), now=T0, known=legacy(C00, C06))
    assert not plan.due and parts(plan) == {"wave": "current", "atmosphere": "current"}


def test_component_part_names_the_domains_of_a_component() -> None:
    from app.services.forecasts.carry import component_part

    whole = om_run(C00, C06).run_key
    assert component_part(whole, wave_key(C06)) == wave_key(C00)
    assert component_part(whole, atmos_key(C00)) == atmos_key(C06)
    assert component_part(wave_key(C00), wave_key(C06)) == wave_key(C00)  # same domains
    assert component_part(wave_key(C00), "gfswave025:2026102700|ecmwf:2026102700") is None
    assert component_part("demo:2026102700", "demo:2026102706") == "demo:2026102700"
    assert component_part("demo:2026102700", "demo:2026102700+wave1") is None
    assert component_part(None, wave_key(C00)) is None


def test_a_metadata_timeout_is_unknown_and_not_due(world: World) -> None:
    world.run = TimeoutError("meta.json timed out")
    plan = plan_sync(None, settings=settings(), now=T0, known=stored(C00, C00))
    assert not plan.due
    (only,) = [c for c in plan.components if c.source_code == SRC]
    assert only.state == "unknown" and only.component is None


def test_coalescing_applies_within_a_component_only(world: World) -> None:
    """GFS 0.13 (wind) published, GFS 0.25 (gusts, pressure) not yet: the atmosphere waits
    for its late domain up to SYNC_COALESCE_MINUTES; the waves are never held back."""
    world.run = om_run(C06, C06, atmos025=C00)
    k = stored(C00, C00)
    plan = plan_sync(None, settings=settings(sync_coalesce_minutes=60), now=T0, known=k)
    assert parts(plan) == {"wave": "new", "atmosphere": "waiting"} and plan.due
    assert "model domains still on an older cycle" in (part(plan, "atmosphere").detail or "")
    later = plan_sync(
        None, settings=settings(sync_coalesce_minutes=60), now=T0 + timedelta(hours=1), known=k
    )
    assert parts(later)["atmosphere"] == "new"
    # Without coalescing (the default, 0) a component is due as soon as a domain advanced.
    now = plan_sync(None, settings=settings(), now=T0, known=stored(C00, C00))
    assert parts(now) == {"wave": "new", "atmosphere": "new"}


def test_a_component_being_published_waits_while_the_other_is_planned(world: World) -> None:
    refused = RunNotServedError("Open-Meteo is publishing ncep_gfswave025", retry_after_s=300.0)
    run = om_run(C00, C06)
    values = {f: getattr(run, f) for f in run.__dataclass_fields__}
    world.run = PartlyServedRun(**values, not_served=MappingProxyType({"wave": refused}))
    plan = plan_sync(None, settings=settings(), now=T0, known=stored(C00, C00))
    assert parts(plan) == {"wave": "waiting", "atmosphere": "new"} and plan.due
    assert (part(plan, "wave").detail or "").startswith("not served yet")


def test_with_several_providers_a_source_is_one_component(world: World) -> None:
    """The blend path (FORECAST_PROVIDERS with two families, design D5): family level."""
    world.run = om_run(C06, C00)
    many = settings(forecast_providers="open_meteo,open_meteo_ecmwf")
    plan = plan_sync(None, settings=many, now=T0, known=stored(C00, C00))
    assert parts(plan) == {None: "new"}


def test_failed_attempts_are_counted_per_component(world: World) -> None:
    world.run = om_run(C06, C06)
    k = stored(C00, C00)
    k.attempts[known_key(f"{SRC}:atmosphere", atmos_key(C06))] = (1, T0)
    plan = plan_sync(None, settings=settings(), now=T0 + timedelta(minutes=5), known=k)
    assert parts(plan) == {"wave": "new", "atmosphere": "waiting"}


# --------------------------------------------------------------------------- spot retries
def retry_known(retries: int, last: datetime, spots: tuple[int, ...] = (3, 7, 9)) -> KnownRuns:
    k = stored(C00, C00)
    k.retries[SRC] = SpotRetry(
        source_code=SRC,
        run_key=k.stored[SRC],
        version_id=12,
        spot_ids=spots,
        retries=retries,
        last_attempt_at=last,
        components=("atmosphere",),
    )
    return k


def test_the_spot_retry_backoff_is_5_15_45_minutes() -> None:
    assert SPOT_RETRY_BACKOFF_MINUTES == (5, 15, 45)
    waits = [spot_retry_wait(n) for n in range(5)]
    assert waits == [timedelta(minutes=m) for m in (5, 15, 45, 45, 45)]


def test_a_retry_of_failed_spots_is_planned_with_backoff(world: World) -> None:
    k = retry_known(0, T0)
    early = plan_sync(None, settings=settings(), now=T0 + timedelta(minutes=4), known=k)
    assert not early.due
    atmos = part(early, "atmosphere")
    assert atmos.state == "waiting" and atmos.retry_spot_ids == (3, 7, 9)
    assert early.waiting_until == T0 + timedelta(minutes=5)
    assert part(early, "wave").retry_spot_ids == ()  # one component carries the retry
    due = plan_sync(None, settings=settings(), now=T0 + timedelta(minutes=5), known=k)
    assert due.due and part(due, "atmosphere").state == "new"
    assert part(due, "atmosphere").retry_spot_ids == (3, 7, 9)
    assert any("retry of 3 failed spots" in r for r in due.reasons)
    # The second retry waits 15 minutes after the first one, the third 45.
    second = retry_known(1, T0)
    assert not plan_sync(
        None, settings=settings(), now=T0 + timedelta(minutes=14), known=second
    ).due
    assert plan_sync(None, settings=settings(), now=T0 + timedelta(minutes=15), known=second).due
    third = retry_known(2, T0)
    assert not plan_sync(None, settings=settings(), now=T0 + timedelta(minutes=44), known=third).due
    assert plan_sync(None, settings=settings(), now=T0 + timedelta(minutes=45), known=third).due


def test_retries_stop_at_the_attempt_limit(world: World) -> None:
    k = retry_known(6, T0)
    plan = plan_sync(None, settings=settings(), now=T0 + timedelta(hours=3), known=k)
    assert not plan.due
    atmos = part(plan, "atmosphere")
    assert atmos.state == "failed" and "SYNC_MAX_ATTEMPTS" in (atmos.detail or "")


def test_a_new_run_supersedes_the_retry(world: World) -> None:
    """A newer atmosphere run refreshes every spot anyway: no separate retry."""
    world.run = om_run(C00, C06)
    plan = plan_sync(
        None, settings=settings(), now=T0 + timedelta(minutes=30), known=retry_known(0, T0)
    )
    atmos = part(plan, "atmosphere")
    assert atmos.state == "new" and atmos.retry_spot_ids == ()


def test_a_retry_of_another_run_is_ignored(world: World) -> None:
    k = retry_known(0, T0)
    k.retries[SRC] = SpotRetry(SRC, "something-older", 3, (1,), 0, T0)
    plan = plan_sync(None, settings=settings(), now=T0 + timedelta(hours=1), known=k)
    assert not plan.due and parts(plan) == {"wave": "current", "atmosphere": "current"}


def test_known_runs_with_retries_round_trip_through_json() -> None:
    k = retry_known(2, T0)
    back = KnownRuns.from_json(k.to_json())
    assert back.retries == k.retries and back.stored == k.stored


# --------------------------------------------------------------------------- runners
def test_limit_plan_restricts_a_plan_to_one_runners_kinds(world: World) -> None:
    world.run = om_run(C06, C00)
    plan = plan_sync(None, settings=settings(), now=T0, known=stored(C00, C00))
    assert plan.due
    # The daemon on Vercel Hobby (SYNC_DAEMON_KINDS=map) leaves the spots to the refresh
    # endpoint: not due for it.
    assert not limit_plan(plan, {"map"}, settings()).due
    spots = limit_plan(plan, {"spots"}, settings())
    assert spots.due and spots.components == plan.components
    # When none of the runner's kinds is a trigger (SYNC_TRIGGER_SOURCES=map), its own
    # kinds trigger: the refresh endpoint still refreshes the spots.
    map_only = settings(sync_trigger_sources="map")
    plan2 = plan_sync(None, settings=map_only, now=T0, known=stored(C00, C00))
    assert limit_plan(plan2, {"spots"}, map_only).due
