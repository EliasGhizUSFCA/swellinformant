"""The automatic refresh pipeline, write side (Swell 2.0 design D6-D10, contract C6).

A refresh detects a new run per component, fetches only the due component, carries the
other one forward from the served run with its ORIGINAL model times, validates every spot,
stages a forecast version and publishes it; a spot whose data failed keeps its previous run
(stale) and is retried alone with backoff; publication is atomic; the refresh endpoint is
secured and idempotent; the kill switch stops every runner; prune keeps every run a version
needs.

Forecast data come from the demo generator (synthetic by design). :class:`SplitDemo` gives
it a wave and an atmosphere run of their own whose cycles the tests set, shifts the values
deterministically by cycle so a refetched component visibly differs (SYNTHETIC), and records
every fetch; no live provider is contacted.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import cli
from app.core.config import get_settings
from app.core.database import get_engine, utcnow
from app.models import ForecastRun, SurfSpot, WaveForecast
from app.models.enums import ForecastRunStatus
from app.models.sync import SyncRun
from app.models.version import ForecastVersion
from app.services.forecasts import ingestion, registry
from app.services.forecasts.base import (
    COMPONENT_FIELDS,
    FetchResult,
    ForecastRecord,
    ModelRunRef,
    RunInfo,
)
from app.services.forecasts.demo import DemoForecastProvider
from app.services.forecasts.open_meteo import PartlyReadRun
from app.services.forecasts.versions import current_version, serving_runs, version_payload
from app.services.http import RunNotServedError, TransientProviderError
from app.services.sync import cli as sync_cli
from app.services.sync import orchestrator, state
from app.services.sync import refresh as refresh_module
from app.services.sync.detect import plan_sync
from tests.conftest import ApiClient
from tests.sync_fixtures import count_queries

pytestmark = pytest.mark.integration

C00 = datetime(2026, 10, 27, 0, tzinfo=UTC)
C06 = datetime(2026, 10, 27, 6, tzinfo=UTC)
SECRET = "s3cret-token-for-the-refresh-route"
AUTH = {"Authorization": f"Bearer {SECRET}"}


class SplitDemo(DemoForecastProvider):
    """SYNTHETIC: the demo generator with a wave and an atmosphere run of their own."""

    cycles: ClassVar[dict[str, datetime]] = {}
    calls: ClassVar[list[tuple[list[str], frozenset[str] | None]]] = []
    failing: ClassVar[set[str]] = set()
    # slug -> f(record, hour index): alters that spot's fetched records (SYNTHETIC).
    tamper: ClassVar[dict[str, Callable[[ForecastRecord, int], None]]] = {}
    entered: ClassVar[threading.Event | None] = None
    gate: ClassVar[threading.Event | None] = None
    # Components whose current run the "provider" is still publishing (SYNTHETIC: what
    # Open-Meteo's meta.json says during the availability wait).
    publishing: ClassVar[set[str]] = set()
    # Components whose metadata cannot be read (SYNTHETIC: their host times out).
    unreadable: ClassVar[set[str]] = set()
    # SYNTHETIC update intervals by component (Open-Meteo reads them from meta.json).
    intervals: ClassVar[dict[str, int]] = {"wave": 21600, "atmosphere": 3600}
    # Data fingerprints by component (SYNTHETIC): when set, the run keys take the shape of
    # the Windy adapter's ("<model>~<fingerprint>:<estimated cycle>", windy.key_part).
    fingerprints: ClassVar[dict[str, str]] = {}

    def update_interval_seconds(self, component: str) -> int | None:
        return self.intervals.get(component)

    def read_metas(self) -> dict[str, Any]:
        return {}

    def ensure_servable(
        self, run: RunInfo, now: datetime | None = None, components: frozenset[str] | None = None
    ) -> dict[str, Any]:
        for ref in run.model_runs:
            wanted = components is None or ref.component in components
            busy = ref.component in self.publishing and ref.init_time == self.cycles[ref.component]
            if wanted and busy:
                raise RunNotServedError(f"synthetic: the {ref.component} run is being published")
        return {}

    def current_run(self, now: datetime, partial: bool = False) -> RunInfo:
        wave, wind = self.cycles["wave"], self.cycles["atmosphere"]
        keys = {"wave": f"demo-wave:{wave:%Y%m%d%H}", "atmosphere": f"demo-wind:{wind:%Y%m%d%H}"}
        if self.fingerprints:
            fp = self.fingerprints
            keys = {
                "wave": f"gfsWave~{fp['wave']}:{wave:%Y%m%d%H}",
                "atmosphere": f"gfs~{fp['atmosphere']}:{wind:%Y%m%d%H}",
            }
        refs = (
            ModelRunRef("demo-swell-generator", wave, component="wave"),
            ModelRunRef("demo-diurnal-wind", wind, component="atmosphere"),
        )
        if self.unreadable:
            unread = {
                c: TransientProviderError(f"synthetic: the {c} meta.json timed out")
                for c in sorted(self.unreadable)
            }
            if not partial or len(unread) == len(keys):
                raise next(iter(unread.values()))
            readable = {c: k for c, k in keys.items() if c not in unread}
            return PartlyReadRun(
                run_key="|".join(readable.values()),
                issued_at=wind if "wave" in unread else wave,
                wave_model_run_at=None if "wave" in unread else wave,
                atmosphere_model_run_at=None if "atmosphere" in unread else wind,
                metadata_source="demo",
                model_runs=tuple(r for r in refs if r.component in readable),
                component_keys=MappingProxyType(readable),
                unread=MappingProxyType(unread),
            )
        return RunInfo(
            run_key=f"{keys['wave']}|{keys['atmosphere']}",
            issued_at=min(wave, wind),
            wave_model_run_at=wave,
            atmosphere_model_run_at=wind,
            metadata_source="demo",
            model_runs=refs,
            component_keys=MappingProxyType(keys),
        )

    def fetch(self, spots: Any, run: RunInfo, components: frozenset[str] | None = None) -> Any:
        self.calls.append((sorted(p.slug for p in spots), components))
        if self.entered is not None and self.gate is not None:
            self.entered.set()
            self.gate.wait(30)
        result: FetchResult = super().fetch(spots, run, components)
        # A visible, deterministic difference per cycle (SYNTHETIC): wave heights grow by
        # the wave cycle's hour, the wind by the atmosphere cycle's hour.
        assert run.wave_model_run_at is not None and run.atmosphere_model_run_at is not None
        wave_hours, wind_hours = run.wave_model_run_at.hour, run.atmosphere_model_run_at.hour
        for recs in result.records.values():
            for rec in recs:
                if rec.values.get("sig_wave_height_m") is not None:
                    rec.values["sig_wave_height_m"] = round(
                        float(rec.values["sig_wave_height_m"] or 0) * (1 + wave_hours / 24), 2
                    )
                if rec.values.get("wind_speed_kmh") is not None:
                    rec.values["wind_speed_kmh"] = round(
                        float(rec.values["wind_speed_kmh"] or 0) + wind_hours, 1
                    )
        for p in spots:
            alter = self.tamper.get(p.slug)
            for i, rec in enumerate(result.records.get(p.spot_id, []) if alter else []):
                alter(rec, i)
            if p.slug in self.failing:  # the request for this spot failed (SYNTHETIC)
                result.records.pop(p.spot_id, None)
                result.errors[p.spot_id] = "synthetic failure: HTTP 502 for this spot"
        return result


@pytest.fixture
def split(monkeypatch: pytest.MonkeyPatch) -> type[SplitDemo]:
    SplitDemo.cycles = {"wave": C00, "atmosphere": C00}
    SplitDemo.calls = []
    SplitDemo.failing = set()
    SplitDemo.tamper = {}
    SplitDemo.publishing = set()
    SplitDemo.unreadable = set()
    SplitDemo.fingerprints = {}
    SplitDemo.entered = SplitDemo.gate = None

    def build(name: str, settings: object = None, overrides: Any = None) -> Any:
        assert name == "demo"
        return SplitDemo(overrides=overrides, natural_swells=False)

    monkeypatch.setattr(registry, "build_provider", build)
    monkeypatch.setattr(ingestion, "build_provider", build)
    return SplitDemo


@pytest.fixture
def client() -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(get_settings(), "cron_secret", SECRET)
    return SECRET


def refresh(now: datetime | None = None) -> dict[str, Any]:
    out = orchestrator.refresh_check(trigger="http", now=now)
    return json.loads(json.dumps(out, default=str))


def spot_ids(db: Session, slugs: list[str]) -> list[int]:
    return sorted(db.scalars(select(SurfSpot.id).where(SurfSpot.slug.in_(slugs))).all())


def active_ids(db: Session) -> list[int]:
    return sorted(db.scalars(select(SurfSpot.id).where(SurfSpot.is_active)).all())


def published(db: Session) -> ForecastVersion:
    db.expire_all()
    version = current_version(db)
    assert version is not None
    return version


def run_of(db: Session, version: ForecastVersion) -> ForecastRun:
    run = db.get(ForecastRun, version.forecast_run_id)
    assert run is not None
    return run


def differing(db: Session, old: int, new: int, fields: tuple[str, ...]) -> int:
    """Rows of run ``new`` whose ``fields`` differ from run ``old`` at the same spot and
    valid time (one statement)."""
    distinct = " OR ".join(f"n.{f} IS DISTINCT FROM o.{f}" for f in fields)
    return int(
        db.scalar(
            text(
                "SELECT count(*) FROM wave_forecasts n JOIN wave_forecasts o"  # noqa: S608
                " ON o.spot_id = n.spot_id AND o.valid_time = n.valid_time"
                f" WHERE n.run_id = :new AND o.run_id = :old AND ({distinct})"
            ),
            {"new": new, "old": old},
        )
        or 0
    )


# --------------------------------------------------------------------------- detection
def test_a_first_refresh_fetches_both_components_and_publishes_a_version(
    db: Session, few_spots: list[str], split: type[SplitDemo]
) -> None:
    out = refresh()
    assert out["status"] == "success", out
    assert len(out["version_ids"]) == 1 and out["sync_run_id"] is not None
    assert {
        c["component"]: c["state"] for c in out["plan"]["components"] if c["kind"] == "spots"
    } == {
        "wave": "new",
        "atmosphere": "new",
    }
    assert split.calls == [(sorted(few_spots), None)]  # one fetch: both components
    v1 = published(db)
    assert v1.id == out["version_ids"][0] and v1.status == "published"
    assert (v1.spots_total, v1.spots_updated, v1.spots_stale, v1.spots_failed) == (5, 5, 0, 0)
    assert v1.provider == "demo" and v1.source_code == "demo"
    assert v1.wave_model_run == C00 and v1.wind_model_run == C00
    assert {c: d["changed"] for c, d in v1.components.items()} == {
        "atmosphere": True,
        "wave": True,
    }
    # Timings: first sighting, acquisition, scoring, staging and publication.
    assert v1.detected_at and v1.acquired_at and v1.scored_at and v1.staged_at
    assert v1.detected_at <= v1.acquired_at <= v1.scored_at <= v1.staged_at <= v1.published_at
    assert v1.timings["kind"] == "refresh" and v1.timings["stages_ms"]["spots"] >= 0
    run = run_of(db, v1)
    assert run.details["component_keys"] == {
        "wave": "demo-wave:2026102700",
        "atmosphere": "demo-wind:2026102700",
    }
    # Every stage of the spot-only sync; the map/GRIB stage never runs on this path.
    stages = {s["name"]: s["status"] for s in out["stages"]}
    assert stages["map"] == "skipped" and stages["spots"] == "success"
    assert stages["publish"] == "success" and stages["predict"] == "success"
    assert out["timings"]["total_ms"] >= out["timings"]["check_ms"] >= 0
    # The version marker in Redis is the published version's payload (C4).
    marker = json.loads(state.get_redis().get(f"{_namespace()}:forecast-version:v1") or "{}")
    assert marker["version_id"] == v1.id and marker["wave_model_run"] == C00.isoformat()


def marker_version() -> int | None:
    raw = state.get_redis().get(f"{_namespace()}:forecast-version:v1")
    return json.loads(raw)["version_id"] if raw else None


def test_the_version_marker_moves_once_the_swell_events_are_re_evaluated(
    db: Session, few_spots: list[str], split: type[SplitDemo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browsers switch to a new version (the version marker) only after detection and
    matching ran on it: event lists fetched for the new version are the new ones."""
    from app.services.matching import service as matching
    from app.services.swell_detection import service as detection

    assert refresh()["status"] == "success"
    v1 = published(db)
    assert marker_version() == v1.id
    seen: dict[str, int | None] = {}
    real_detect, real_match = detection.detect_events, matching.match_all

    def detect(*a: Any, **k: Any) -> Any:
        seen["detect"] = marker_version()
        return real_detect(*a, **k)

    def match(*a: Any, **k: Any) -> Any:
        seen["match"] = marker_version()
        return real_match(*a, **k)

    monkeypatch.setattr(detection, "detect_events", detect)
    monkeypatch.setattr(matching, "match_all", match)
    split.cycles["wave"] = C06
    out = refresh()
    assert out["status"] == "success", out
    v2 = published(db)
    assert v2.id != v1.id
    assert seen == {"detect": v1.id, "match": v1.id}  # the new version was not announced yet
    assert marker_version() == v2.id


def _namespace() -> str:
    from app.services.public_cache import namespace

    return namespace()


@pytest.mark.parametrize(("changed", "kept"), [("atmosphere", "wave"), ("wave", "atmosphere")])
def test_a_one_component_update_keeps_the_other_components_values_and_model_time(
    db: Session, few_spots: list[str], split: type[SplitDemo], changed: str, kept: str
) -> None:
    assert refresh()["status"] == "success"
    v1 = published(db)
    r1 = run_of(db, v1)
    split.cycles[changed] = C06  # e.g. GFS 06Z wind published, GFS-Wave still 00Z
    out = refresh()
    assert out["status"] == "success", out
    spots_plan = {
        c["component"]: c["state"] for c in out["plan"]["components"] if c["kind"] == "spots"
    }
    assert spots_plan == {changed: "new", kept: "current"}
    # One fetch, of the changed component only, for every spot.
    assert split.calls[-1] == (sorted(few_spots), frozenset({changed}))
    v2 = published(db)
    r2 = run_of(db, v2)
    assert r2.id != r1.id and v2.previous_version_id == v1.id
    db.refresh(v1)
    assert v1.status == "superseded" and v1.superseded_at is not None
    # The carried component keeps its ORIGINAL model run time, in the run and the version.
    times = {"wave": "wave_model_run_at", "atmosphere": "atmosphere_model_run_at"}
    assert getattr(r2, times[kept]) == C00 and getattr(r2, times[changed]) == C06
    assert (v2.wave_model_run, v2.wind_model_run) == (
        (C06, C00) if changed == "wave" else (C00, C06)
    )
    payload = version_payload(v2)
    assert payload["wave_model_run"] == v2.wave_model_run.isoformat()  # type: ignore[union-attr]
    assert payload["wind_model_run"] == v2.wind_model_run.isoformat()  # type: ignore[union-attr]
    assert v2.components[kept]["changed"] is False
    assert v2.components[kept]["run_key"] == v1.components[kept]["run_key"]
    assert v2.components[kept]["retrieved_at"] == v1.components[kept]["retrieved_at"]
    assert v2.components[changed]["changed"] is True
    details = r2.details["components"]
    assert details[kept] == {
        "changed": False,
        "run_key": r1.details["component_keys"][kept],
        "retrieved_at": r1.details["components"][kept]["retrieved_at"],
        "carried_from_run_id": r1.id,
    }
    assert details[changed]["changed"] is True and details[changed]["carried_from_run_id"] is None
    assert {m["component"]: m["init_time"] for m in r2.details["model_runs"]} == {
        kept: C00.isoformat(),
        changed: C06.isoformat(),
    }
    # The values: the carried component is copied unchanged, the fetched one is new.
    assert differing(db, r1.id, r2.id, COMPONENT_FIELDS[kept]) == 0
    assert differing(db, r1.id, r2.id, COMPONENT_FIELDS[changed]) > 0
    rows = db.scalar(select(func.count()).where(WaveForecast.run_id == r2.id))
    assert rows == db.scalar(select(func.count()).where(WaveForecast.run_id == r1.id))
    hollow = db.scalar(
        select(func.count()).where(
            WaveForecast.run_id == r2.id, WaveForecast.sig_wave_height_m.is_(None)
        )
    )
    assert hollow == 0  # every hour has its carried or fetched wave height
    # Scores were recomputed for the whole new run.
    assert r2.predictions_computed_at is not None


def test_a_composed_run_names_each_components_own_model_run_in_the_api(
    db: Session, few_spots: list[str], split: type[SplitDemo], client: TestClient
) -> None:
    """Review r1-frontend-FE-1: after a wave-only refresh the waves come from the 06Z run and
    the wind from the 00Z run. The API names both, each with its own init and fetch time,
    never only the older one (spec 10: correct source timestamps)."""
    assert refresh()["status"] == "success"
    split.cycles["wave"] = C06
    assert refresh()["status"] == "success"
    r2 = run_of(db, published(db))
    timeline = client.get("/api/spots/pipeline/timeline").json()
    members = timeline["provenance"]["members"]
    by = {m["component"]: m for m in members}
    assert set(by) == {"wave", "atmosphere"}
    assert by["wave"]["init_time"].startswith("2026-10-27T06:00")
    assert by["atmosphere"]["init_time"].startswith("2026-10-27T00:00")
    assert by["wave"]["run_key"] == "demo-wave:2026102706"
    assert by["atmosphere"]["run_key"] == "demo-wind:2026102700"
    fetched = r2.details["components"]

    def at(text: str) -> datetime:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))

    for c in ("wave", "atmosphere"):
        assert at(by[c]["retrieved_at"]) == at(fetched[c]["retrieved_at"])
    assert at(by["wave"]["retrieved_at"]) > at(by["atmosphere"]["retrieved_at"])
    forecast = client.get("/api/spots/pipeline/forecast").json()
    assert {m["component"] for m in forecast["run"]["members"]} == {"wave", "atmosphere"}
    current = client.get("/api/spots/pipeline").json()["current"]
    assert current["wave_run_issued_at"].startswith("2026-10-27T06:00")
    assert current["atmosphere_run_issued_at"].startswith("2026-10-27T00:00")
    # Both components of one run: one entry, as before.
    split.cycles["atmosphere"] = C06
    assert refresh()["status"] == "success"
    members = client.get("/api/spots/pipeline/timeline").json()["provenance"]["members"]
    assert len(members) == 1 and members[0]["component"] is None


def test_a_run_stored_before_components_existed_is_split_by_its_model_domains(
    db: Session, few_spots: list[str], split: type[SplitDemo]
) -> None:
    """The upgrade path: the stored run predates component keys (Upgrade 4) and no version
    exists yet. Each component's stored key is the part of the run key that names its own
    model domains, so a new atmosphere run is the only new component, and the waves are
    carried from that run with their own model time; an older wave run is never counted as
    new, and the refresh never fails for want of a carried key."""
    split.cycles["atmosphere"] = C06  # the stored run: waves of 00Z, wind of 06Z
    assert refresh()["status"] == "success"
    r1 = run_of(db, published(db))
    # The run as Upgrade 4 stored it: no component keys or parts, untagged model runs.
    legacy = {k: v for k, v in r1.details.items() if k not in ("component_keys", "components")}
    legacy["model_runs"] = [
        {k: v for k, v in m.items() if k != "component"} for m in legacy["model_runs"]
    ]
    r1.details = legacy
    db.execute(text("DELETE FROM forecast_versions"))
    db.commit()
    state.get_redis().delete(state.KNOWN_KEY)
    fetches = len(split.calls)
    split.cycles["atmosphere"] = datetime(2026, 10, 27, 12, tzinfo=UTC)
    out = refresh()
    assert out["status"] == "success", out
    plan = {c["component"]: c["state"] for c in out["plan"]["components"] if c["kind"] == "spots"}
    assert plan == {"wave": "current", "atmosphere": "new"}
    assert split.calls[fetches:] == [(sorted(few_spots), frozenset({"atmosphere"}))]
    v2 = published(db)
    r2 = run_of(db, v2)
    assert (v2.wave_model_run, v2.wind_model_run) == (C00, datetime(2026, 10, 27, 12, tzinfo=UTC))
    assert r2.wave_model_run_at == C00 and v2.components["wave"]["changed"] is False
    assert v2.components["wave"]["run_key"] == "demo-wave:2026102700"
    assert r2.details["components"]["wave"]["carried_from_run_id"] == r1.id
    assert {m["component"]: m["init_time"] for m in r2.details["model_runs"]} == {
        "wave": C00.isoformat(),
        "atmosphere": datetime(2026, 10, 27, 12, tzinfo=UTC).isoformat(),
    }
    assert differing(db, r1.id, r2.id, COMPONENT_FIELDS["wave"]) == 0


def test_a_component_still_being_published_is_carried_with_its_published_run(
    db: Session, few_spots: list[str], split: type[SplitDemo]
) -> None:
    """New wave and wind runs, but the wind is still being published (as Open-Meteo's GFS
    0.25 during its availability wait): the waves are refreshed alone and the wind is
    carried with the run it was published with, never labelled with the newer run."""
    assert refresh()["status"] == "success"
    r1 = run_of(db, published(db))
    split.cycles = {"wave": C06, "atmosphere": C06}
    split.publishing = {"atmosphere"}
    out = refresh()
    assert out["status"] == "success", out
    plan = {c["component"]: c["state"] for c in out["plan"]["components"] if c["kind"] == "spots"}
    assert plan == {"wave": "new", "atmosphere": "waiting"}
    assert split.calls[-1] == (sorted(few_spots), frozenset({"wave"}))
    v2 = published(db)
    r2 = run_of(db, v2)
    assert (v2.wave_model_run, v2.wind_model_run) == (C06, C00)
    assert (r2.wave_model_run_at, r2.atmosphere_model_run_at) == (C06, C00)
    assert v2.components["atmosphere"]["run_key"] == "demo-wind:2026102700"
    assert v2.components["atmosphere"]["changed"] is False
    assert r2.details["component_keys"]["atmosphere"] == "demo-wind:2026102700"
    assert differing(db, r1.id, r2.id, COMPONENT_FIELDS["atmosphere"]) == 0
    # Once the wind run is served, it is refreshed alone.
    split.publishing = set()
    out = refresh()
    assert out["status"] == "success", out
    assert split.calls[-1] == (sorted(few_spots), frozenset({"atmosphere"}))
    v3 = published(db)
    assert (v3.wave_model_run, v3.wind_model_run) == (C06, C06)


def test_a_component_whose_metadata_cannot_be_read_is_carried_and_the_other_refreshed(
    db: Session, few_spots: list[str], split: type[SplitDemo]
) -> None:
    """D6: the wave host timing out does not hold a new wind run back. The waves are
    unknown in the plan (never due, never "current"), carried with their published run;
    the wind is fetched and published; the check record names the unknown component."""
    assert refresh()["status"] == "success"
    v1 = published(db)
    r1 = run_of(db, v1)
    assert v1.components["wave"]["update_interval_seconds"] == 21600  # SYNTHETIC intervals
    assert v1.components["atmosphere"]["update_interval_seconds"] == 3600
    split.unreadable = {"wave"}
    split.cycles["atmosphere"] = C06
    out = refresh()
    assert out["status"] == "success", out
    spots_plan = {
        c["component"]: c["state"] for c in out["plan"]["components"] if c["kind"] == "spots"
    }
    assert spots_plan == {"wave": "unknown", "atmosphere": "new"}
    assert split.calls[-1] == (sorted(few_spots), frozenset({"atmosphere"}))
    v2 = published(db)
    r2 = run_of(db, v2)
    assert (v2.wave_model_run, v2.wind_model_run) == (C00, C06)
    assert v2.components["wave"]["changed"] is False
    assert v2.components["wave"]["run_key"] == v1.components["wave"]["run_key"]
    assert r2.wave_model_run_at == C00 and r2.details["component_keys"] == {
        "wave": f"demo-wave:{C00:%Y%m%d%H}",
        "atmosphere": f"demo-wind:{C06:%Y%m%d%H}",
    }
    assert differing(db, r1.id, r2.id, COMPONENT_FIELDS["wave"]) == 0
    assert v2.spots_updated == len(few_spots)
    # Monitoring sees the unknown component in the refresh check's record (design D12).
    last = state.read_refresh_check()
    assert last is not None
    wave = next(c for c in last["components"] if c.get("component") == "wave")
    assert wave["state"] == "unknown" and "timed out" in wave["detail"]
    # Nothing readable: unknown, nothing due, nothing fetched.
    split.unreadable = {"wave", "atmosphere"}
    fetches = len(split.calls)
    assert refresh()["status"] == "not_due"
    assert len(split.calls) == fetches


def test_a_first_refresh_without_one_components_metadata_publishes_nothing(
    db: Session, few_spots: list[str], split: type[SplitDemo]
) -> None:
    """Nothing published yet and the wave metadata unreadable: there is no wave run to
    carry, so the refresh fails (retried) and publishes nothing; once the metadata answers
    both components are fetched."""
    split.unreadable = {"wave"}
    out = refresh()
    assert out["status"] == "failed" and out["version_ids"] == [], out
    assert current_version(db) is None and split.calls == []
    (spots,) = [s for s in out["stages"] if s["name"] == "spots"]
    assert spots["status"] == "failed"
    row = db.get(SyncRun, out["sync_run_id"])
    assert row is not None
    (stage,) = [s for s in row.stages if s["name"] == "spots"]
    (atmosphere,) = [c for c in stage["detail"]["components"] if c["component"] == "atmosphere"]
    assert atmosphere["outcome"] == "failed"
    assert "wave metadata could not be read" in atmosphere["message"]
    split.unreadable = set()
    again = refresh()
    assert again["status"] == "success", again
    assert split.calls == [(sorted(few_spots), None)]


def test_a_carried_component_keeps_its_own_extra_keys(
    db: Session, few_spots: list[str], split: type[SplitDemo]
) -> None:
    assert refresh()["status"] == "success"
    r1 = run_of(db, published(db))
    # The provider labels of the first run (SYNTHETIC values, the keys real adapters write).
    labels = {
        "gust_kind": "gfs_instantaneous",
        "wind_cell": {"lat": 1.0, "lon": 2.0, "distance_km": 3.0},
        "pressure_kind": "msl",
        "wave_cell": {"lat": 4.0, "lon": 5.0, "distance_km": 6.0},
    }
    db.execute(
        text("UPDATE wave_forecasts SET extra = extra || CAST(:labels AS jsonb) WHERE run_id = :r"),
        {"labels": json.dumps(labels), "r": r1.id},
    )
    db.commit()
    split.cycles["wave"] = C06  # a new wave run: the atmosphere is carried
    assert refresh()["status"] == "success"
    r2 = run_of(db, published(db))
    assert r2.id != r1.id
    extras = db.scalars(select(WaveForecast.extra).where(WaveForecast.run_id == r2.id)).all()
    assert extras and all(
        {k: (e or {}).get(k) for k in ("gust_kind", "wind_cell", "pressure_kind")}
        == {k: labels[k] for k in ("gust_kind", "wind_cell", "pressure_kind")}
        for e in extras
    )
    # The wave labels belong to the fetched component: never copied from the old run.
    assert not any("wave_cell" in (e or {}) for e in extras)


def test_an_unchanged_run_is_not_refreshed_and_the_check_reads_no_database(
    db: Session, few_spots: list[str], split: type[SplitDemo]
) -> None:
    assert refresh()["status"] == "success"
    fetches = len(split.calls)
    counter, remove = count_queries(get_engine())
    try:
        out = refresh()
    finally:
        remove()
    assert out["status"] == "not_due" and not out["due"]
    assert len(split.calls) == fetches  # zero data requests
    assert counter.total == 0, counter.statements  # metadata and Redis only
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ForecastVersion)) == 1
    assert db.scalar(select(func.count()).select_from(SyncRun)) == 1


def test_a_windy_run_seen_again_under_a_newer_estimated_label_is_not_refetched(
    db: Session, few_spots: list[str], split: type[SplitDemo]
) -> None:
    """Windy has no model-run field: a run is a data fingerprint with an estimated cycle
    label (design D3). When the adapter's run state is lost (Redis emptied, a cold process),
    unchanged data are seen again under a newer label: the same run, never refetched or
    published again, also by a forced sync; a new fingerprint still is."""
    split.fingerprints = {"wave": "aaaa-bbbb-cccc", "atmosphere": "dddd-eeee-ffff"}
    assert refresh()["status"] == "success"
    v1 = published(db)
    fetches = len(split.calls)
    split.cycles["wave"] = C06  # the same wave data, relabelled one cycle newer
    out = refresh()
    assert out["status"] == "not_due" and not out["due"]
    assert len(split.calls) == fetches
    forced = orchestrator.run_sync(trigger="cli", force=True)
    assert forced.status != "failed"
    assert len(split.calls) == fetches  # not even a forced sync refetches it
    db.expire_all()
    assert published(db).id == v1.id
    # A new atmosphere fingerprint: only the atmosphere is fetched; the waves keep the
    # stored run and its original (00Z) label.
    split.fingerprints = {"wave": "aaaa-bbbb-cccc", "atmosphere": "0123-4567-89ab"}
    split.cycles["atmosphere"] = C06
    assert refresh()["status"] == "success"
    assert {c for _, c in split.calls[fetches:]} == {frozenset({"atmosphere"})}
    v2 = published(db)
    assert v2.id != v1.id
    assert v2.wave_model_run == C00 and v2.wind_model_run == C06
    assert v2.components["wave"]["run_key"] == "gfsWave~aaaa-bbbb-cccc:2026102700"


class TwoDomainAtmosphere(SplitDemo):
    """SYNTHETIC: the atmosphere component has two model domains (GFS 0.13 and 0.25 deg),
    as Open-Meteo's GFS family does; the test sets each domain's cycle."""

    domains: ClassVar[dict[str, datetime]] = {}

    def current_run(self, now: datetime, partial: bool = False) -> RunInfo:
        wave = self.cycles["wave"]
        g13, g25 = self.domains["gfs013"], self.domains["gfs025"]
        keys = {
            "wave": f"gfswave025:{wave:%Y%m%d%H}",
            "atmosphere": f"gfs013:{g13:%Y%m%d%H}|gfs025:{g25:%Y%m%d%H}",
        }
        return RunInfo(
            run_key=f"{keys['wave']}|{keys['atmosphere']}",
            issued_at=min(wave, g13, g25),
            wave_model_run_at=wave,
            atmosphere_model_run_at=min(g13, g25),
            metadata_source="demo",
            model_runs=(
                ModelRunRef("gfswave025", wave, component="wave"),
                ModelRunRef("gfs013", g13, component="atmosphere"),
                ModelRunRef("gfs025", g25, component="atmosphere"),
            ),
            component_keys=MappingProxyType(keys),
        )


def test_a_lagging_domain_never_publishes_an_older_atmosphere_run(
    db: Session, few_spots: list[str], split: type[SplitDemo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r1-backend-F1: GFS 0.13 advanced to 06Z while a lagging metadata server
    answers GFS 0.25 with the cycle BEFORE the published 00Z. Monotonic per domain: nothing
    is fetched or published (a run built from it would name 18Z as its wind run), and the
    atmosphere is refreshed once every domain is at or past its published cycle."""
    TwoDomainAtmosphere.domains = {"gfs013": C00, "gfs025": C00}

    def build(name: str, settings: object = None, overrides: Any = None) -> Any:
        return TwoDomainAtmosphere(overrides=overrides, natural_swells=False)

    monkeypatch.setattr(registry, "build_provider", build)
    monkeypatch.setattr(ingestion, "build_provider", build)
    assert refresh()["status"] == "success"
    v1 = published(db)
    assert v1.wind_model_run == C00
    fetches = len(split.calls)
    TwoDomainAtmosphere.domains = {"gfs013": C06, "gfs025": C00 - timedelta(hours=6)}
    out = refresh()
    assert out["status"] == "not_due" and not out["due"], out
    (atmosphere,) = [c for c in out["plan"]["components"] if c["component"] == "atmosphere"]
    assert atmosphere["state"] == "current"
    assert "older than the stored run" in (atmosphere["detail"] or "")
    assert len(split.calls) == fetches
    assert published(db).id == v1.id
    # Also when the refresh runs anyway (a forced sync): the component is not refetched.
    forced = orchestrator.run_sync(trigger="cli", force=True, only=orchestrator.REFRESH_KINDS)
    assert forced.status != "failed"
    assert {c for _, c in split.calls[fetches:]} <= {frozenset({"wave"})}
    assert published(db).wind_model_run == C00
    # The lagging domain catches up: the new atmosphere run is refreshed, never older.
    TwoDomainAtmosphere.domains = {"gfs013": C06, "gfs025": C06}
    out = refresh()
    assert out["status"] == "success", out
    v2 = published(db)
    assert v2.id != v1.id and v2.wind_model_run == C06


class GfsSteps(SplitDemo):
    """SYNTHETIC: GFS-style native steps (hourly to 120 h, 3-hourly after) counted from
    each component's own model run, as Open-Meteo stores them: a component 6 h out of phase
    with the carried one has hours the carried run lacks (its leads 121, 122, 124, 125 h)."""

    @staticmethod
    def _native(t: datetime, init: datetime) -> bool:
        lead = (t - init).total_seconds() / 3600
        return lead >= 0 and lead == int(lead) and (lead <= 120 or int(lead) % 3 == 0)

    def fetch(self, spots: Any, run: RunInfo, components: frozenset[str] | None = None) -> Any:
        result = super().fetch(spots, run, components)
        assert run.wave_model_run_at is not None and run.atmosphere_model_run_at is not None
        init = (
            run.atmosphere_model_run_at
            if components == frozenset({"atmosphere"})
            else run.wave_model_run_at
        )
        for spot_id, recs in result.records.items():
            result.records[spot_id] = [r for r in recs if self._native(r.valid_time, init)]
        return result


@pytest.mark.parametrize(("changed", "kept"), [("atmosphere", "wave"), ("wave", "atmosphere")])
def test_no_published_hour_lacks_the_carried_component(
    db: Session,
    few_spots: list[str],
    split: type[SplitDemo],
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
    kept: str,
) -> None:
    """Review r1-backend-F4: the fetched component's hours that the carried run does not
    have (the runs step differently) are not published half filled: no published hour lacks
    a wave height or a wind speed, and none is scored as flat water."""
    from app.models import SurfQualityPrediction

    def build(name: str, settings: object = None, overrides: Any = None) -> Any:
        return GfsSteps(overrides=overrides, natural_swells=False)

    monkeypatch.setattr(registry, "build_provider", build)
    monkeypatch.setattr(ingestion, "build_provider", build)
    assert refresh()["status"] == "success"
    r1 = run_of(db, published(db))
    split.cycles[changed] = C06
    out = refresh()
    assert out["status"] == "success", out
    v2 = published(db)
    r2 = run_of(db, v2)
    assert r2.id != r1.id and v2.components[kept]["changed"] is False
    column = {"wave": WaveForecast.sig_wave_height_m, "atmosphere": WaveForecast.wind_speed_kmh}
    for field in column.values():
        hollow = db.scalar(
            select(func.count()).where(WaveForecast.run_id == r2.id, field.is_(None))
        )
        assert hollow == 0, f"{hollow} published hours without {field.key}"
    # Every hour of the new run is an hour of the carried run (the 4 hours between 120 h
    # and 126 h of the carried run that only the fetched run has were left out).
    extra_hours = db.scalar(
        text(
            "SELECT count(*) FROM wave_forecasts n WHERE n.run_id = :new AND NOT EXISTS ("
            " SELECT 1 FROM wave_forecasts o WHERE o.run_id = :old AND o.spot_id = n.spot_id"
            " AND o.valid_time = n.valid_time)"
        ),
        {"new": r2.id, "old": r1.id},
    )
    assert extra_hours == 0
    flat = db.scalar(
        select(func.count()).where(
            SurfQualityPrediction.run_id == r2.id,
            SurfQualityPrediction.score == 0,
            SurfQualityPrediction.forecast_id.in_(
                select(WaveForecast.id).where(
                    WaveForecast.run_id == r2.id, WaveForecast.sig_wave_height_m.is_(None)
                )
            ),
        )
    )
    assert flat == 0


def test_an_hour_without_any_wave_height_is_never_scored(
    db: Session, few_spots: list[str], split: type[SplitDemo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth (review r1-backend-F4): an hour that reached a run without a wave
    height of any kind has no score, never "poor" with score 0 (unknown waves are not
    flat water)."""
    from app.models import SurfQualityPrediction

    def build(name: str, settings: object = None, overrides: Any = None) -> Any:
        return GfsSteps(overrides=overrides, natural_swells=False)

    monkeypatch.setattr(registry, "build_provider", build)
    monkeypatch.setattr(ingestion, "build_provider", build)
    assert refresh()["status"] == "success"
    # As before the fix: the hours the carried wave run lacks are kept half filled.
    monkeypatch.setattr(
        ingestion.carry, "drop_uncarried_hours", lambda db, **kwargs: 0, raising=True
    )
    split.cycles["atmosphere"] = C06
    assert refresh()["status"] == "success"
    r2 = run_of(db, published(db))
    hollow = list(
        db.scalars(
            select(WaveForecast.id).where(
                WaveForecast.run_id == r2.id, WaveForecast.sig_wave_height_m.is_(None)
            )
        )
    )
    assert len(hollow) == 4 * len(few_spots)  # leads 121, 122, 124 and 125 h of the waves
    scored = db.scalar(select(func.count()).where(SurfQualityPrediction.forecast_id.in_(hollow)))
    assert scored == 0
    assert db.scalar(select(func.count()).where(SurfQualityPrediction.run_id == r2.id)) > 0


def test_a_version_marker_lost_after_publication_is_written_again_by_the_next_sync(
    db: Session, few_spots: list[str], split: type[SplitDemo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r1-backend-F2: a process killed between the publication and the marker write
    (after "match") left the previous version's marker, which has no expiry. The next sync
    that takes the lease finds it behind the published version and writes it again."""
    from app.services.forecasts import serving

    assert refresh()["status"] == "success"
    v1 = published(db)
    split.cycles["wave"] = C06
    with monkeypatch.context() as m:  # the process dies after the publication committed
        m.setattr(orchestrator, "_write_marker", lambda ctx: None)
        m.setattr(orchestrator, "remember", lambda known, result, now=None: known)
        refresh()
    v2 = published(db)
    assert v2.id != v1.id and marker_version() == v1.id
    out = refresh(utcnow() + timedelta(minutes=5))
    assert out["sync_run_id"] is not None, out  # the run seen again takes the lease
    assert marker_version() == v2.id
    serving.clear_memo()
    assert serving.version_marker(db)["version_id"] == v2.id
    # Nothing new afterwards: the checks read no database and leave the marker alone.
    assert refresh(utcnow() + timedelta(minutes=10))["status"] == "not_due"
    assert marker_version() == v2.id


def test_the_refresh_check_asks_only_the_spot_providers(
    db: Session, few_spots: list[str], split: type[SplitDemo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The map layers (standalone mode, as in the hosted profile) belong to another runner:
    the refresh endpoint's check neither asks NOAA for them nor marks them due."""
    from app.services.forecasts import noaa

    asked: list[datetime] = []

    def probed(_self: object, now: datetime) -> RunInfo:
        asked.append(now)
        raise AssertionError("the map provider was asked")

    monkeypatch.setattr(get_settings(), "map_fields_enabled", True)
    monkeypatch.setattr(noaa.NoaaGfsFieldsProvider, "current_run", probed)
    out = refresh()
    assert out["status"] == "success", out
    assert asked == []
    (layers,) = [c for c in out["plan"]["components"] if c["kind"] == "map"]
    assert layers["state"] == "disabled" and "not checked here" in layers["detail"]
    assert {s["name"]: s["status"] for s in out["stages"]}["map"] == "skipped"
    # The daemon (every kind) still checks the map layers.
    known = state.read_known()
    assert known is not None
    plan = plan_sync(None, known=known)
    assert len(asked) == 1
    assert next(c for c in plan.components if c.kind == "map").state == "unknown"


# --------------------------------------------------------------------------- failures
def test_47_of_50_spots_publish_the_3_failed_keep_their_run_and_are_retried_alone(
    db: Session, split: type[SplitDemo]
) -> None:
    every = active_ids(db)
    assert len(every) == 50
    assert refresh()["status"] == "success"
    v1 = published(db)
    r1 = run_of(db, v1)
    three = ["pipeline", "uluwatu", "nazare"]
    failed_ids = spot_ids(db, three)
    split.failing = set(three)
    split.cycles["atmosphere"] = C06
    out = refresh()
    assert out["status"] == "partial", out  # the sync is partial
    v2 = published(db)
    r2 = run_of(db, v2)
    assert (v2.spots_total, v2.spots_updated, v2.spots_stale, v2.spots_failed) == (50, 47, 3, 3)
    entries = v2.spot_runs
    for spot_id in failed_ids:
        entry = entries[str(spot_id)]
        assert entry["status"] == "stale" and entry["run_id"] == r1.id  # previous run kept
        assert "synthetic failure" in entry["error"]
    assert {int(s) for s, e in entries.items() if e["status"] == "updated"} == set(every) - set(
        failed_ids
    )
    serving = serving_runs(db, v2)
    assert {serving[s] for s in failed_ids} == {r1.id}
    assert {serving[s] for s in set(every) - set(failed_ids)} == {r2.id}
    assert (
        r2.status == ForecastRunStatus.PARTIAL
        and sorted(r2.details["failed_spot_ids"]) == failed_ids
    )
    # Each failed spot's own error, by id (a sync that reuses the run stages the same).
    assert ingestion.stored_spot_errors(r2.details) == {
        s: entries[str(s)]["error"] for s in failed_ids
    }
    stored_failed = db.scalar(
        select(func.count()).where(
            WaveForecast.run_id == r2.id, WaveForecast.spot_id.in_(failed_ids)
        )
    )
    assert stored_failed == 0  # incomplete records are never published as complete
    # A retry of only those 3 spots is planned, 5 minutes later.
    known = state.read_known()
    assert known is not None
    soon = plan_sync(None, now=utcnow() + timedelta(minutes=1), known=known)
    atmos = next(c for c in soon.components if c.component == "atmosphere")
    assert atmos.state == "waiting" and list(atmos.retry_spot_ids) == failed_ids
    assert not soon.due
    # The provider answers again; the retry fetches only the 3 spots and publishes a
    # version that updates just them.
    split.failing = set()
    fetches = len(split.calls)
    retry = refresh(now=utcnow() + timedelta(minutes=6))
    assert retry["status"] == "success", retry
    assert split.calls[fetches:] == [(sorted(three), frozenset({"atmosphere"}))]
    v3 = published(db)
    r3 = run_of(db, v3)
    assert r3.run_key == f"{r2.run_key}#retry1" and v3.timings["kind"] == "retry"
    assert (v3.spots_total, v3.spots_updated, v3.spots_stale, v3.spots_failed) == (50, 3, 0, 0)
    for spot_id in failed_ids:
        assert v3.spot_runs[str(spot_id)]["run_id"] == r3.id
        assert v3.spot_runs[str(spot_id)]["status"] == "updated"
    others = {s: e for s, e in v3.spot_runs.items() if int(s) not in failed_ids}
    assert others == {s: e for s, e in v2.spot_runs.items() if int(s) not in failed_ids}
    assert v3.components == v2.components  # the same model runs: only spots were retried
    # The retried spots carry the waves of the run they were served from (same wave key).
    assert differing(db, r1.id, r3.id, COMPONENT_FIELDS["wave"]) == 0
    # Nothing is left to retry.
    known = state.read_known()
    assert known is not None and known.retries == {}
    assert refresh(now=utcnow() + timedelta(minutes=30))["status"] == "not_due"


def test_retries_of_failed_spots_back_off_and_stop_at_the_attempt_limit(
    db: Session, few_spots: list[str], split: type[SplitDemo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D9 with the counts in the database: each retry that fails again waits longer
    (5, then 15 minutes), and after SYNC_MAX_ATTEMPTS retries the spot stays stale until
    the next run."""
    monkeypatch.setattr(get_settings(), "sync_max_attempts", 2)
    assert refresh()["status"] == "success"
    split.failing = {"pipeline"}
    split.cycles["atmosphere"] = C06
    assert refresh()["status"] == "partial"
    staged = published(db).staged_at
    assert staged is not None

    def retry_state(at: datetime) -> tuple[str, int]:
        known = state.read_known()
        assert known is not None
        plan = plan_sync(None, now=at, known=known)
        atmos = next(c for c in plan.components if c.component == "atmosphere")
        return atmos.state, atmos.attempts

    assert retry_state(staged + timedelta(minutes=4)) == ("waiting", 0)
    first = refresh(now=staged + timedelta(minutes=6))  # the first retry fails again
    assert first["status"] == "failed", first
    tried = utcnow()  # the retry run's start (the wall clock)
    assert retry_state(tried + timedelta(minutes=14)) == ("waiting", 1)  # 15 minutes now
    assert retry_state(tried + timedelta(minutes=16)) == ("new", 1)
    second = refresh(now=tried + timedelta(minutes=16))
    assert second["status"] == "failed", second
    assert retry_state(utcnow() + timedelta(hours=5)) == ("failed", 2)  # SYNC_MAX_ATTEMPTS
    fetches = len(split.calls)
    assert refresh(now=utcnow() + timedelta(hours=5))["status"] == "not_due"
    assert len(split.calls) == fetches
    pipeline = spot_ids(db, ["pipeline"])[0]
    assert published(db).spot_runs[str(pipeline)]["status"] == "stale"


def test_a_spot_whose_data_fail_validation_keeps_its_previous_run(
    db: Session, few_spots: list[str], split: type[SplitDemo]
) -> None:
    """D9 validation before staging: a value outside the physical ranges, or fewer than
    FORECAST_MIN_VALID_FRACTION of the hours with a wave height and a wind speed, fails
    that spot alone (stale on its previous run, the reason recorded, retried)."""
    assert refresh()["status"] == "success"
    r1 = run_of(db, published(db))

    def decoding_error(rec: ForecastRecord, i: int) -> None:
        if i == 10:
            rec.values["sig_wave_height_m"] = 45.0  # SYNTHETIC: above the 30 m bound

    def gappy(rec: ForecastRecord, i: int) -> None:
        if i % 3 == 0:
            rec.values["sig_wave_height_m"] = None  # SYNTHETIC: a third of the hours

    split.tamper = {"pipeline": decoding_error, "nazare": gappy}
    split.cycles["wave"] = C06
    out = refresh()
    assert out["status"] == "partial", out
    v2 = published(db)
    r2 = run_of(db, v2)
    ids = dict(db.execute(select(SurfSpot.slug, SurfSpot.id).where(SurfSpot.is_active)).all())
    entries = v2.spot_runs
    assert entries[str(ids["pipeline"])]["error"] == (
        "values outside physical ranges: sig_wave_height_m"
    )
    assert "FORECAST_MIN_VALID_FRACTION 0.8" in entries[str(ids["nazare"])]["error"]
    for slug in ("pipeline", "nazare"):
        assert entries[str(ids[slug])]["status"] == "stale"
        assert entries[str(ids[slug])]["run_id"] == r1.id
    assert (v2.spots_updated, v2.spots_stale) == (3, 2)
    stored = db.scalar(
        select(func.count()).where(
            WaveForecast.run_id == r2.id,
            WaveForecast.spot_id.in_([ids["pipeline"], ids["nazare"]]),
        )
    )
    assert stored == 0  # the failed spots' rows are not in the new run
    known = state.read_known()
    assert known is not None and known.retries["demo"].spot_ids == tuple(
        sorted((ids["pipeline"], ids["nazare"]))
    )


def test_a_whole_component_failure_publishes_nothing_and_keeps_everything_served(
    db: Session, few_spots: list[str], split: type[SplitDemo]
) -> None:
    assert refresh()["status"] == "success"
    v1 = published(db)
    split.failing = set(few_spots)
    split.cycles["atmosphere"] = C06
    out = refresh()
    assert out["status"] == "failed" and out["version_ids"] == [] and out["published_at"] is None
    assert published(db).id == v1.id
    failed_run = db.scalar(
        select(ForecastRun).where(ForecastRun.status == ForecastRunStatus.FAILED)
    )
    assert failed_run is not None and "no spot returned usable data" in (
        failed_run.error_message or ""
    )
    assert set(serving_runs(db).values()) == {v1.forecast_run_id}
    known = state.read_known()
    assert known is not None
    assert known.attempts[f"demo:atmosphere|demo-wind:{C06:%Y%m%d%H}"][0] == 1
    assert not any(k.startswith("demo:wave|") for k in known.attempts)


def test_a_failure_after_staging_leaves_the_previous_version_published(
    db: Session, few_spots: list[str], split: type[SplitDemo], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert refresh()["status"] == "success"
    v1 = published(db)

    def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("synthetic: the publication transaction failed")

    monkeypatch.setattr(refresh_module, "publish_version", broken)
    split.cycles["wave"] = C06
    out = refresh()
    assert out["status"] == "failed" and out["published_at"] is None
    assert published(db).id == v1.id and published(db).status == "published"
    staged = db.scalar(select(ForecastVersion).where(ForecastVersion.id != v1.id))
    assert staged is not None and staged.status == "failed"
    assert "publication transaction failed" in (staged.error_message or "")
    assert set(serving_runs(db).values()) == {v1.forecast_run_id}
    row = db.get(SyncRun, out["sync_run_id"])
    assert row is not None and row.published_at is None and row.status == "failed"


# --------------------------------------------------------------------------- concurrency
def test_two_refresh_calls_at_once_make_one_sync_and_the_other_is_busy(
    db: Session, few_spots: list[str], split: type[SplitDemo]
) -> None:
    split.entered, split.gate = threading.Event(), threading.Event()
    results: dict[str, dict[str, Any]] = {}

    def first() -> None:
        results["first"] = refresh()

    worker = threading.Thread(target=first)
    worker.start()
    try:
        assert split.entered.wait(30)  # the first refresh holds the lease, mid-fetch
        split.entered = None
        # Its lease is the endpoint's short one (a stopped Function's lease expires soon).
        assert get_settings().sync_lease_minutes > orchestrator.HTTP_LEASE_MINUTES
        db.expire_all()
        running = db.scalar(select(SyncRun).where(SyncRun.status == "running"))
        assert running is not None and running.trigger == "http"
        assert running.lease_expires_at is not None
        assert running.lease_expires_at <= utcnow() + timedelta(
            minutes=orchestrator.HTTP_LEASE_MINUTES
        )
        results["second"] = refresh()
    finally:
        split.gate.set()
        worker.join(60)
    assert results["second"]["status"] == "busy"
    assert results["first"]["status"] == "success"
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(SyncRun)) == 1
    assert db.scalar(select(func.count()).select_from(ForecastVersion)) == 1
    assert len(split.calls) == 1  # one acquisition
    # A repeated delivery afterwards finds nothing due (idempotent).
    assert refresh()["status"] == "not_due"


# --------------------------------------------------------------------------- runners
def test_the_refresh_route_needs_the_cron_secret(
    db: Session,
    few_spots: list[str],
    split: type[SplitDemo],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.internal import REFRESH_PATH

    monkeypatch.setattr(get_settings(), "cron_secret", None)
    assert client.get(REFRESH_PATH, headers=AUTH).status_code == 404
    assert client.post(REFRESH_PATH, headers=AUTH).status_code == 404
    monkeypatch.setattr(get_settings(), "cron_secret", SECRET)
    assert client.post(REFRESH_PATH).status_code == 401
    assert client.get(REFRESH_PATH, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post(REFRESH_PATH, headers={"Authorization": SECRET}).status_code == 401
    assert split.calls == []  # nothing ran without the secret
    resp = client.post(REFRESH_PATH, headers=AUTH)
    assert resp.status_code == 200 and resp.headers["cache-control"] == "private, no-store"
    body = resp.json()
    assert body["status"] == "success" and len(body["version_ids"]) == 1
    assert {"plan", "sync_run_id", "published_at", "stages", "refreshes", "timings"} <= set(body)
    assert body["refreshes"][0]["spots_updated"] == 5
    again = client.get(REFRESH_PATH, headers=AUTH)
    assert again.status_code == 200 and again.json()["status"] == "not_due"
    # Every check is recorded for the scheduler-silence alert (design D12).
    last = state.read_refresh_check()
    assert last is not None and last["status"] == "not_due" and last["caller"] == "refresh"
    assert datetime.fromisoformat(last["at"]) >= utcnow() - timedelta(minutes=1)


def test_the_kill_switch_stops_every_runner(
    db: Session,
    few_spots: list[str],
    split: type[SplitDemo],
    client: TestClient,
    secret: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.api.internal import CRON_DAILY_PATH, REFRESH_PATH
    from app.services.sync.daemon import SyncDaemon

    monkeypatch.setattr(get_settings(), "forecast_refresh_enabled", False)
    # The refresh endpoint.
    resp = client.post(REFRESH_PATH, headers=AUTH)
    assert resp.status_code == 200 and resp.json()["status"] == "disabled"
    last = state.read_refresh_check()
    assert last is not None and last["status"] == "disabled"
    # The daily cron's backstop.
    daily = client.get(CRON_DAILY_PATH, headers=AUTH).json()
    assert daily["refresh"]["status"] == "disabled"
    # Celery's tick and `sync run --if-due`.
    assert orchestrator.tick(trigger="celery")["status"] == "disabled"
    # The command line.
    assert cli.main(["sync", "run"]) == sync_cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == "disabled"
    assert cli.main(["sync", "run", "--force"]) == sync_cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == "disabled"
    # The daemon: it checks (heartbeat) but never syncs.
    daemon = SyncDaemon(get_settings())
    daemon.start(init_db=False)
    plan = daemon.check()
    assert not plan.due and orchestrator.DISABLED_REASON in plan.reasons
    heartbeat = state.read_heartbeat()
    assert heartbeat is not None and heartbeat.note == "refreshes disabled"
    assert split.calls == []
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(SyncRun)) == 0
    assert db.scalar(select(func.count()).select_from(ForecastVersion)) == 0


def test_the_daemon_syncs_only_its_component_kinds(
    db: Session, few_spots: list[str], split: type[SplitDemo], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.sync.daemon import SyncDaemon

    monkeypatch.setattr(get_settings(), "sync_daemon_kinds", "map")
    daemon = SyncDaemon(get_settings())
    daemon.start(init_db=False)
    plan = daemon.check()
    assert not plan.due  # spot runs are new, but the refresh endpoint syncs those
    assert any("synced by another runner" in r for r in plan.reasons)
    assert split.calls == []
    monkeypatch.setattr(get_settings(), "sync_daemon_kinds", "spots")
    daemon = SyncDaemon(get_settings())
    daemon.start(init_db=False)
    assert daemon.check().due
    assert published(db).spots_updated == 5


def test_the_daily_cron_runs_one_refresh_check(
    db: Session, few_spots: list[str], split: type[SplitDemo], client: TestClient, secret: str
) -> None:
    from app.api.internal import CRON_DAILY_PATH

    body = client.get(CRON_DAILY_PATH, headers=AUTH).json()
    assert body["status"] in {"ok", "degraded", "failing"}
    assert body["refresh"]["status"] == "success" and len(body["refresh"]["version_ids"]) == 1
    assert published(db).id == body["refresh"]["version_ids"][0]
    last = state.read_refresh_check()
    assert last is not None and last["caller"] == "cron_daily" and last["status"] == "success"


# --------------------------------------------------------------------------- simulation (C6)
def test_simulate_model_run_advances_one_demo_component(
    db: Session, few_spots: list[str], app_client: ApiClient, client: TestClient, secret: str
) -> None:
    """The developer tool behind the live-update test: the real demo provider offers a new
    simulated wave run; the refresh fetches the waves only and publishes them, visibly
    different, with the wind carried."""
    from app.api.internal import REFRESH_PATH

    assert client.post(REFRESH_PATH, headers=AUTH).json()["status"] == "success"
    v1 = published(db)
    r1 = run_of(db, v1)
    assert app_client.post("/api/dev/simulate-model-run?component=wave").status_code == 401
    app_client.register()
    resp = app_client.post("/api/dev/simulate-model-run?component=wave")
    assert resp.status_code == 200, resp.text
    sim = resp.json()
    assert sim["advances"] == {"wave": 1} and sim["component_keys"]["wave"].endswith("+wave1")
    assert app_client.post("/api/dev/simulate-model-run?component=tide").status_code == 422
    body = client.post(REFRESH_PATH, headers=AUTH).json()
    assert body["status"] == "success", body
    plan = {c["component"]: c["state"] for c in body["plan"]["components"] if c["kind"] == "spots"}
    assert plan == {"wave": "new", "atmosphere": "current"}
    v2 = published(db)
    r2 = run_of(db, v2)
    assert v2.components["wave"]["changed"] and not v2.components["atmosphere"]["changed"]
    assert differing(db, r1.id, r2.id, COMPONENT_FIELDS["atmosphere"]) == 0
    ratio = db.scalar(
        text(
            "SELECT avg(n.sig_wave_height_m / o.sig_wave_height_m) FROM wave_forecasts n"
            " JOIN wave_forecasts o ON o.spot_id = n.spot_id AND o.valid_time = n.valid_time"
            " WHERE n.run_id = :new AND o.run_id = :old AND o.sig_wave_height_m > 0.5"
        ),
        {"new": r2.id, "old": r1.id},
    )
    assert ratio == pytest.approx(1.35, abs=0.02)  # the first advance's wave step


# --------------------------------------------------------------------------- retention
def test_prune_keeps_every_run_a_version_needs(
    db: Session, few_spots: list[str], split: type[SplitDemo], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.maintenance import prune_pass

    assert refresh()["status"] == "success"
    v1 = published(db)
    r1 = run_of(db, v1)
    stale = spot_ids(db, ["pipeline"])
    split.failing = {"pipeline"}
    split.cycles["atmosphere"] = C06
    assert refresh()["status"] == "partial"
    v2 = published(db)
    r2 = run_of(db, v2)
    split.failing = set()
    split.cycles["wave"] = C06
    assert refresh()["status"] == "success"  # pipeline refreshed fully: both changed
    v3 = published(db)
    # Pretend every run and version is far past retention (1 day).
    monkeypatch.setattr(get_settings(), "retention_forecast_days", 1)
    old = utcnow() - timedelta(days=5)
    db.execute(text("UPDATE forecast_runs SET started_at = :t"), {"t": old})
    db.execute(text("UPDATE forecast_versions SET created_at = :t"), {"t": old})
    db.commit()
    # An old run no version references, and an old superseded version.
    orphan = ForecastRun(
        source_id=r1.source_id,
        run_key="demo-wave:2026102500|demo-wind:2026102500",
        issued_at=old,
        started_at=old,
        completed_at=old,
        status=ForecastRunStatus.SUCCESS,
        details={"spot_ids": []},
    )
    db.add(orphan)
    db.commit()
    out = prune_pass(db)
    db.expire_all()
    remaining = set(db.scalars(select(ForecastRun.id)).all())
    assert orphan.id not in remaining
    # v3 (published) and v2 (the previous published) and every run they reference stay,
    # among them r1, which v2 still serves to the stale spot.
    assert v2.spot_runs[str(stale[0])]["run_id"] == r1.id
    assert {r1.id, r2.id, v3.forecast_run_id} <= remaining
    versions = set(db.scalars(select(ForecastVersion.id)).all())
    assert versions == {v2.id, v3.id} and out["forecast_versions"] == 1  # v1 pruned
    # A version left staging (a sync stopped between staging and publishing) protects its
    # run while it may still be published; once older than the retention it is abandoned.
    run_ids = set(db.scalars(select(ForecastRun.id)).all())
    stray = ForecastRun(
        source_id=r1.source_id,
        run_key="demo-wave:2026102506|demo-wind:2026102506",
        issued_at=old,
        started_at=old,
        completed_at=old,
        status=ForecastRunStatus.SUCCESS,
        details={"spot_ids": []},
    )
    db.add(stray)
    db.commit()
    staging = ForecastVersion(
        status="staging",
        provider="demo",
        source_code="demo",
        forecast_run_id=stray.id,
        previous_version_id=v3.id,
        staged_at=utcnow(),
    )
    db.add(staging)
    db.commit()
    prune_pass(db)
    db.expire_all()
    assert stray.id in set(db.scalars(select(ForecastRun.id)).all())  # staging: kept
    assert db.get(ForecastVersion, staging.id) is not None
    db.execute(
        text("UPDATE forecast_versions SET staged_at = :t, created_at = :t WHERE id = :v"),
        {"t": old, "v": staging.id},
    )
    db.commit()
    assert prune_pass(db)["forecast_versions"] == 1  # abandoned: failed, then pruned
    prune_pass(db)
    db.expire_all()
    assert set(db.scalars(select(ForecastRun.id)).all()) == run_ids  # its run went too
    assert published(db).id == v3.id


# --------------------------------------------------------------------------- map layers
def test_a_component_refresh_stores_the_map_fields_a_noaa_run_reads(db: Session) -> None:
    """The "with_noaa" map mode (FORECAST_PROVIDERS=noaa_gfs, one provider, so the spot stage
    is a component refresh): the map fields the direct GRIB ingester reads with its spot run
    are stored with that run, as by the family-level acquisition (SYNTHETIC GRIB files)."""
    from app.services.forecasts.base import COMPONENTS
    from app.services.forecasts.noaa import NoaaGfsProvider
    from tests.integration.test_map_fields import HOURS, KEEP, NOW, keep_spots, stored
    from tests.synthetic_gfs import SyntheticGfs
    from tests.synthetic_map_fields import field_settings

    keep_spots(db, KEEP)
    world = SyntheticGfs()
    world.add_cycle(datetime(2026, 10, 10, 18, tzinfo=UTC), HOURS)
    provider = NoaaGfsProvider(field_settings(), client=world.client(), backoff_seconds=0)
    assert provider.map_fields
    outcome = ingestion.refresh_source(
        db, provider, token="noaa_gfs", due=COMPONENTS, stored={}, serving={}, now=NOW
    )
    assert outcome.status == "success", outcome
    assert outcome.changed == COMPONENTS and len(outcome.updated_spots) == len(KEEP)
    rows = stored(db)
    assert len(rows) == 18 and {r.run_id for r in rows} == {outcome.run_id}
    run = db.get(ForecastRun, outcome.run_id)
    assert run is not None and run.details["map_fields"]["count"] == 18
