"""app.services.forecasts.versions, the parts that need no database (contracts C3/C4): the
component and spot records, the freshness rule of design D10 with the per-model override,
the version payload and the Redis helpers failing open. Every model run, time and spot
below is SYNTHETIC."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import redis

from app.core.config import Settings
from app.models.version import ForecastVersion
from app.services.forecasts import versions
from app.services.forecasts.base import ATMOSPHERE, WAVE, ModelRunRef
from app.services.forecasts.versions import (
    ComponentRun,
    DomainRun,
    MapRunRef,
    SpotFreshness,
    SpotRun,
    component_runs_of,
    spot_freshness,
    spot_runs_of,
    version_payload,
)

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
C06 = datetime(2026, 10, 27, 6, tzinfo=UTC)
C00 = datetime(2026, 10, 27, 0, tzinfo=UTC)

C4_KEYS = {
    "version_id",
    "published_at",
    "provider",
    "source_code",
    "wind_model",
    "wind_model_run",
    "wind_run_estimated",
    "wave_model",
    "wave_model_run",
    "wave_run_estimated",
    "spots_total",
    "spots_updated",
    "spots_stale",
    "map_run_id",
    "map_cycle",
    "attribution",
}


def settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type,call-arg]


def component(name: str, retrieved_at: datetime | None, **kw: object) -> ComponentRun:
    model = "ncep_gfswave025" if name == WAVE else "ncep_gfs013,ncep_gfs025"
    return ComponentRun(
        component=name,
        model=str(kw.pop("model", model)),
        run_key=f"{name}:2026102706",
        init_time=C06,
        available_at=C06 + timedelta(hours=5),
        retrieved_at=retrieved_at,
        **kw,  # type: ignore[arg-type]
    )


def version(
    components: list[ComponentRun], spot_runs: dict[int, SpotRun], **kw: object
) -> ForecastVersion:
    """A transient (never stored) version row."""
    return ForecastVersion(
        id=kw.pop("id", 7),
        status=kw.pop("status", "published"),
        components={c.component: c.as_dict() for c in components},
        spot_runs={str(k): v.as_dict() for k, v in spot_runs.items()},
        **kw,
    )


# --------------------------------------------------------------------------- records
def test_component_runs_round_trip() -> None:
    original = ComponentRun(
        component=ATMOSPHERE,
        model="ncep_gfs013,ncep_gfs025",
        run_key="gfs:2026102706",
        init_time=C06,
        available_at=C06 + timedelta(hours=4, minutes=40),
        retrieved_at=NOW,
        changed=False,
        carried_from_run_id=41,
        estimated=True,
        update_interval_seconds=21_600,
        domains=(
            DomainRun("ncep_gfs013", C06, C06 + timedelta(hours=4)),
            DomainRun("ncep_gfs025", C06),
        ),
    )
    data = original.as_dict()
    assert data["init_time"] == "2026-10-27T06:00:00+00:00"
    assert data["domains"][1] == {
        "model": "ncep_gfs025",
        "init_time": "2026-10-27T06:00:00+00:00",
        "available_at": None,
    }
    assert ComponentRun.from_dict(ATMOSPHERE, data) == original
    with pytest.raises(ValueError, match="unknown component"):
        ComponentRun(component="tide")


def test_a_component_is_built_from_the_providers_model_runs() -> None:
    refs = [
        ModelRunRef("ncep_gfswave025", C06, C06 + timedelta(hours=5), WAVE),
        ModelRunRef("ncep_gfs013", C06, C06 + timedelta(hours=4), ATMOSPHERE),
        ModelRunRef("ncep_gfs025", C00, C00 + timedelta(hours=5), ATMOSPHERE),
        ModelRunRef("shared_model", C06),  # no component: counts for both
    ]
    atmosphere = ComponentRun.from_model_runs(ATMOSPHERE, refs, run_key="a", retrieved_at=NOW)
    assert atmosphere.model == "ncep_gfs013,ncep_gfs025,shared_model"
    assert atmosphere.init_time == C00  # the oldest domain: the data are that old
    assert atmosphere.available_at == C06 + timedelta(hours=4)  # the newest availability
    assert [d.model for d in atmosphere.domains] == ["ncep_gfs013", "ncep_gfs025", "shared_model"]
    wave = ComponentRun.from_model_runs(WAVE, refs, changed=False, carried_from_run_id=3)
    assert wave.model == "ncep_gfswave025,shared_model" and wave.init_time == C06
    assert wave.changed is False and wave.carried_from_run_id == 3
    empty = ComponentRun.from_model_runs(WAVE, [])
    assert empty.model is None and empty.init_time is None and empty.domains == ()


def test_spot_runs_round_trip_and_refuse_unknown_states() -> None:
    entry = SpotRun(12, "stale", C06, "timeout")
    assert entry.as_dict() == {
        "run_id": 12,
        "status": "stale",
        "last_updated_at": "2026-10-27T06:00:00+00:00",
        "error": "timeout",
    }
    assert SpotRun.from_dict(entry.as_dict()) == entry
    assert SpotRun.from_dict({"run_id": None, "status": "missing"}) == SpotRun(None, "missing")
    with pytest.raises(ValueError, match="unknown spot status"):
        SpotRun(1, "failed")


def test_a_versions_json_reads_back() -> None:
    v = version([component(WAVE, NOW)], {3: SpotRun(9, "updated", NOW)})
    assert spot_runs_of(v) == {3: SpotRun(9, "updated", NOW)}
    assert component_runs_of(v)[WAVE].retrieved_at == NOW
    assert spot_runs_of(ForecastVersion(spot_runs={})) == {}


# --------------------------------------------------------------------------- freshness
def test_fresh_within_the_interval_plus_the_threshold() -> None:
    s = settings(forecast_stale_threshold_minutes=360)
    retrieved = NOW - timedelta(hours=11, minutes=59)  # 6 h interval + 360 min, minus 1 min
    v = version(
        [component(WAVE, retrieved), component(ATMOSPHERE, retrieved)],
        {1: SpotRun(5, "updated", retrieved)},
    )
    assert spot_freshness(v, NOW, s) == {1: SpotFreshness(retrieved, False, None)}
    later = NOW + timedelta(minutes=2)
    assert spot_freshness(v, later, s)[1] == SpotFreshness(retrieved, True, "atmosphere_age")


def test_the_model_update_interval_counts() -> None:
    retrieved = NOW - timedelta(hours=8)
    hourly = component(WAVE, retrieved, update_interval_seconds=3600)
    v = version([hourly], {1: SpotRun(5, "updated", retrieved)})
    s = settings(forecast_stale_threshold_minutes=360)  # 1 h + 6 h < 8 h
    assert spot_freshness(v, NOW, s)[1].reason == "wave_age"
    slow = version([component(WAVE, retrieved)], {1: SpotRun(5, "updated", retrieved)})
    assert spot_freshness(slow, NOW, s)[1].stale is False  # 6 h + 6 h > 8 h


def test_the_per_model_override_applies_to_its_model_only() -> None:
    retrieved = NOW - timedelta(hours=7)
    v = version(
        [component(WAVE, retrieved), component(ATMOSPHERE, retrieved)],
        {1: SpotRun(5, "updated", retrieved)},
    )
    base = settings(forecast_stale_threshold_minutes=360)
    assert spot_freshness(v, NOW, base)[1].stale is False
    tight_wave = settings(
        forecast_stale_threshold_minutes=360,
        forecast_stale_threshold_minutes_by_model='{"ncep_gfswave025": 30}',
    )
    assert spot_freshness(v, NOW, tight_wave)[1] == SpotFreshness(retrieved, True, "wave_age")
    # An override names a domain model of a component too.
    tight_domain = settings(forecast_stale_threshold_minutes_by_model='{"ncep_gfs025": 30}')
    v_domains = version(
        [
            component(
                ATMOSPHERE,
                retrieved,
                domains=(DomainRun("ncep_gfs013", C06), DomainRun("ncep_gfs025", C06)),
            )
        ],
        {1: SpotRun(5, "updated", retrieved)},
    )
    assert spot_freshness(v_domains, NOW, tight_domain)[1].reason == "atmosphere_age"
    # A lenient override keeps an otherwise stale model fresh.
    old = NOW - timedelta(hours=20)
    lenient = settings(forecast_stale_threshold_minutes_by_model='{"ncep_gfswave025": 1440}')
    v_old = version([component(WAVE, old)], {1: SpotRun(5, "updated", old)})
    assert spot_freshness(v_old, NOW, lenient)[1].stale is False
    assert spot_freshness(v_old, NOW, base)[1].stale is True


def test_a_carried_component_ages_from_its_original_retrieval() -> None:
    s = settings(forecast_stale_threshold_minutes=360)
    fresh_wave = component(WAVE, NOW - timedelta(hours=1))
    carried_wind = component(
        ATMOSPHERE, NOW - timedelta(hours=13), changed=False, carried_from_run_id=4
    )
    v = version([fresh_wave, carried_wind], {1: SpotRun(8, "updated", NOW - timedelta(hours=1))})
    out = spot_freshness(v, NOW, s)[1]
    # The spot was updated an hour ago, but its wind data are 13 h old: never labelled current.
    assert out.stale and out.reason == "atmosphere_age"
    assert out.last_updated_at == NOW - timedelta(hours=1)


def test_a_spot_carried_from_an_older_refresh_ages_from_its_own_update() -> None:
    s = settings(forecast_stale_threshold_minutes=360)
    # A retry of other spots just fetched both components; spot 2 was last updated 13 h ago.
    v = version(
        [component(WAVE, NOW), component(ATMOSPHERE, NOW)],
        {1: SpotRun(9, "updated", NOW), 2: SpotRun(4, "updated", NOW - timedelta(hours=13))},
    )
    out = spot_freshness(v, NOW, s)
    assert out[1].stale is False
    assert out[2].stale is True and out[2].reason == "atmosphere_age"


def test_failed_and_missing_spots_are_stale_whatever_their_age() -> None:
    s = settings()
    v = version(
        [component(WAVE, NOW)],
        {
            1: SpotRun(5, "stale", NOW - timedelta(minutes=30), "HTTP 502"),
            2: SpotRun(None, "missing", None, "no data"),
        },
    )
    out = spot_freshness(v, NOW, s)
    assert out[1] == SpotFreshness(NOW - timedelta(minutes=30), True, "refresh_failed")
    assert out[2] == SpotFreshness(None, True, "missing")


def test_without_components_the_spots_own_update_counts() -> None:
    s = settings(forecast_stale_threshold_minutes=60)
    v = version([], {1: SpotRun(5, "updated", NOW - timedelta(hours=6, minutes=59))})
    assert spot_freshness(v, NOW, s)[1].stale is False
    assert spot_freshness(v, NOW + timedelta(minutes=2), s)[1].reason == "age"
    assert spot_freshness(version([], {1: SpotRun(5, "updated", None)}), NOW, s)[1].stale
    assert spot_freshness(None, NOW, s) == {}


# --------------------------------------------------------------------------- payload
def test_the_payload_has_exactly_the_c4_fields() -> None:
    v = version(
        [component(WAVE, NOW), component(ATMOSPHERE, NOW, estimated=True)],
        {},
        id=42,
        published_at=NOW,
        provider="open_meteo",
        source_code="open_meteo_noaa",
        wind_model="ncep_gfs013,ncep_gfs025",
        wind_model_run=C06,
        wind_run_estimated=False,
        wave_model="ncep_gfswave025",
        wave_model_run=C00,
        wave_run_estimated=True,
        spots_total=50,
        spots_updated=47,
        spots_stale=3,
    )
    payload = version_payload(v, MapRunRef(run_id=9, cycle=C06))
    assert set(payload) == C4_KEYS
    assert payload == {
        "version_id": 42,
        "published_at": "2026-10-27T12:00:00+00:00",
        "provider": "open_meteo",
        "source_code": "open_meteo_noaa",
        "wind_model": "ncep_gfs013,ncep_gfs025",
        "wind_model_run": "2026-10-27T06:00:00+00:00",
        "wind_run_estimated": False,
        "wave_model": "ncep_gfswave025",
        "wave_model_run": "2026-10-27T00:00:00+00:00",
        "wave_run_estimated": True,
        "spots_total": 50,
        "spots_updated": 47,
        "spots_stale": 3,
        "map_run_id": 9,
        "map_cycle": "2026-10-27T06:00:00+00:00",
        "attribution": [
            "Weather data by Open-Meteo.com (CC BY 4.0)",
            "NOAA NCEP GFS and GFS-Wave (modified)",
        ],
    }


def test_the_payload_before_any_publication() -> None:
    payload = version_payload(None)
    assert set(payload) == C4_KEYS
    assert payload["version_id"] is None and payload["published_at"] is None
    assert payload["spots_total"] == payload["spots_updated"] == payload["spots_stale"] == 0
    assert payload["wind_run_estimated"] is False and payload["attribution"] == []
    assert payload["map_run_id"] is None and payload["map_cycle"] is None
    # A map run is reported even before the first forecast version (map layers publish alone).
    assert version_payload(None, MapRunRef(3))["map_run_id"] == 3


def test_the_payload_takes_a_forecast_run_as_the_map_run() -> None:
    from app.models import ForecastRun

    run = ForecastRun(id=11, issued_at=C06)
    payload = version_payload(None, run)
    assert payload["map_run_id"] == 11 and payload["map_cycle"] == "2026-10-27T06:00:00+00:00"


@pytest.mark.parametrize(
    ("provider", "text"),
    [
        ("windy", "Contains data from the Windy database"),
        ("open_meteo", "Weather data by Open-Meteo.com (CC BY 4.0)"),
        ("demo", "Synthetic demo data generated by Swell Travel Agent, not a real forecast"),
    ],
)
def test_the_attribution_follows_the_provider(provider: str, text: str) -> None:
    assert text in versions.attribution_for(provider)
    v = version([], {}, provider=provider, published_at=NOW)
    assert text in version_payload(v)["attribution"]
    assert versions.attribution_for("unknown") == [] and versions.attribution_for(None) == []


# --------------------------------------------------------------------------- Redis
class DownRedis:
    """A Redis client whose every command fails as an unreachable server does."""

    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, name: str) -> object:
        def fail(*_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise redis.ConnectionError("Connection refused (synthetic)")

        return fail


@pytest.fixture
def down(monkeypatch: pytest.MonkeyPatch) -> DownRedis:
    client = DownRedis()
    monkeypatch.setattr(versions, "get_redis", lambda: client)
    versions.reset_fail_open()
    yield client
    versions.reset_fail_open()


def test_every_redis_helper_fails_open(down: DownRedis) -> None:
    payload = version_payload(None)
    assert versions.write_version_marker(payload) is False
    versions.reset_fail_open()
    assert versions.read_version_marker() is None
    versions.reset_fail_open()
    assert versions.note_first_served(7, NOW) is False
    versions.reset_fail_open()
    assert versions.first_served_at(7) is None
    versions.reset_fail_open()
    assert versions.record_seen(7, NOW, published_at=NOW) is None
    versions.reset_fail_open()
    assert versions.seen_latencies() == []
    assert down.calls == 6


def test_after_a_failure_redis_is_left_alone_for_a_while(
    down: DownRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(versions.time, "monotonic", lambda: clock[0])
    assert versions.read_version_marker() is None
    assert down.calls == 1
    assert versions.read_version_marker() is None
    assert versions.write_version_marker({"version_id": 1}) is False
    assert versions.seen_latencies() == []
    assert down.calls == 1  # not asked again within FAIL_OPEN_SECONDS
    clock[0] += versions.FAIL_OPEN_SECONDS + 1
    assert versions.read_version_marker() is None
    assert down.calls == 2


def test_the_keys_live_in_the_public_cache_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VERCEL_ENV", raising=False)
    assert versions.marker_key() == "public:v1:forecast-version:v1"
    assert versions.first_served_key(12) == "public:v1:forecast-version:first-served:12"
    assert versions.seen_latency_key() == "public:v1:forecast-version:seen-latency"
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("VERCEL_DEPLOYMENT_ID", "dpl_synthetic")
    assert versions.marker_key().startswith("public:v1:preview:")
    assert versions.marker_key().endswith(":forecast-version:v1")
