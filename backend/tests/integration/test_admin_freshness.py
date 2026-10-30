"""GET /api/admin/freshness (design D12, contract C5) against PostgreSQL and Redis.

Admin only (401 signed out, 403 for a user, never cached), and every section-16 metric is
computed from what the pipeline records: versions, syncs, forecast runs with their fetch
stats, Redis counters and latencies, flight searches and emails. All rows are SYNTHETIC
(tests/admin_fixtures.py), modelled on measured Open-Meteo GFS timings.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.core.redis import consume_quota
from app.models import EmailMessage, FlightSearch, Notification, SurfSpot, User
from app.models.enums import (
    CabinClass,
    EmailDeliveryStatus,
    EmailPurpose,
    FlightSearchStatus,
    ForecastRunStatus,
    NotificationChannel,
    NotificationKind,
    NotificationStatus,
)
from app.services.forecasts import quota, versions
from tests import admin_fixtures as fx
from tests.conftest import ApiClient

pytestmark = pytest.mark.integration

PATH = "/api/admin/freshness"


@pytest.fixture
def open_meteo(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment under test serves Open-Meteo (no request is made: rows are written)."""
    monkeypatch.setattr(get_settings(), "forecast_providers", "open_meteo")


def admin_client(app_client: ApiClient, db: Session) -> ApiClient:
    app_client.register(email="ops@example.com")
    user = db.scalar(select(User).where(User.email == "ops@example.com"))
    assert user is not None
    user.is_admin = True
    db.commit()
    return app_client


def test_the_dashboard_is_for_admins_only_and_never_cached(
    app_client: ApiClient, db: Session
) -> None:
    anonymous = app_client.get(PATH)
    assert anonymous.status_code == 401
    assert "no-store" in anonymous.headers["cache-control"]
    app_client.register()
    user = app_client.get(PATH)
    assert user.status_code == 403
    assert user.json()["error"]["message"] == "The freshness dashboard is for admin accounts."
    admin = db.scalar(select(User).where(User.email == "surfer@example.com"))
    assert admin is not None
    admin.is_admin = True
    db.commit()
    ok = app_client.get(PATH)
    assert ok.status_code == 200
    assert ok.headers["cache-control"] == "private, no-store"
    assert "cdn-cache-control" not in ok.headers
    body = ok.json()
    # Before any version (the upgrade path): honest empty figures and a note saying why.
    assert body["version_id"] is None and body["last_published_version"] is None
    assert body["stale_spots"] == [] and body["spots_updated"] is None
    assert body["atmosphere"]["run"] is None and body["wave"]["typical_lag_minutes"] is None
    assert body["notes"][0].startswith("No forecast version has been published yet")
    assert body["latency"]["publication_to_frontend_s"]["samples"] == 0


def _flight(db: Session, provider: str, status: FlightSearchStatus, when: object) -> None:
    db.add(
        FlightSearch(
            provider=provider,
            cache_key=f"synthetic:{provider}:{status.value}:{when}",
            origin_iata="SFO",
            destination_iata="DPS",
            departure_date=utcnow().date() + timedelta(days=5),
            return_date=utcnow().date() + timedelta(days=12),
            adults=1,
            cabin_class=CabinClass.ECONOMY,
            currency="USD",
            max_connections=2,
            status=status,
            searched_at=when,
            expires_at=utcnow() + timedelta(hours=6),
            is_mock=provider == "demo",
        )
    )


def test_every_metric_comes_from_the_recorded_pipeline(
    app_client: ApiClient, db: Session, open_meteo: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = admin_client(app_client, db)
    now = utcnow()
    c2 = fx.cycle_of(now - fx.WIND_LAG - timedelta(minutes=30))  # newest available cycle
    c1 = c2 - timedelta(hours=6)
    # The wind below is carried from c1, so it is older than the newest cycle by up to 12.5 h
    # depending on the wall clock's place in the 6 h cycle; with the default 6 h + 360 min
    # limit it turned every spot stale ("atmosphere_age") in the last ~30 minutes of each
    # cycle. A 420-minute threshold keeps it fresh at any time of day, so the stale spots
    # are exactly the three whose refresh failed (the carried-component age rule itself is
    # pinned in tests/unit/test_forecast_versions.py).
    monkeypatch.setattr(get_settings(), "forecast_stale_threshold_minutes", 420)
    spots = fx.active_spot_ids(db)
    failed_spots = {spots[0]: "masked cells", spots[1]: "HTTP 500", spots[2]: "timeout"}

    # Two refreshes: c1 both components, then a wave-only refresh of c2 carrying the wind.
    sync1 = fx.sync_run(db, c1 + fx.WIND_LAG + timedelta(minutes=11))
    run1 = fx.forecast_run(
        db,
        c1,
        f"open_meteo_noaa:{c1:%Y%m%d%H}",
        stats={
            "requests": 4,
            "http_status_counts": {"200": 4},
            "weighted_calls": 340.0,
            "bytes_downloaded": 1_020_000,
        },
        started=now - timedelta(hours=7),
    )
    v1 = fx.publish(
        db,
        run1,
        [fx.wave(c1), fx.wind(c1)],
        published_at=c1 + fx.WIND_LAG + timedelta(minutes=12, seconds=30),
        sync_run_id=sync1.id,
    )
    sync2 = fx.sync_run(db, c2 + fx.WAVE_LAG + timedelta(minutes=11))
    run2 = fx.forecast_run(
        db,
        c2,
        f"open_meteo_noaa:{c2:%Y%m%d%H}#wave",
        stats={"requests": 3, "http_status_counts": {"200": 2, "429": 1}, "weighted_calls": 170.0},
        started=now - timedelta(hours=1),
        wind_run=c1,  # the wind is carried from c1
    )
    published_at = c2 + fx.WAVE_LAG + timedelta(minutes=12, seconds=25)
    v2 = fx.publish(
        db,
        run2,
        [fx.wave(c2), fx.wind(c1, changed=False, carried_from=run1.id)],
        published_at=published_at,
        stale=failed_spots,
        sync_run_id=sync2.id,
    )
    # A failed atmosphere attempt and a map-only daemon sync.
    fx.forecast_run(
        db,
        c2,
        f"open_meteo_noaa:{c2:%Y%m%d%H}#atmosphere",
        status=ForecastRunStatus.FAILED,
        stats={"requests": 2, "http_status_counts": {"500": 2}},
        error="Open-Meteo HTTP 500: internal error",
        started=now - timedelta(minutes=40),
    )
    sync3 = fx.sync_run(
        db,
        now - timedelta(minutes=15),
        status="failed",
        stages=(("spots", 4100, 0.4),),
        error="TransientProviderError: Open-Meteo HTTP 500 at api.open-meteo.com",
        published=False,
    )
    map_sync = fx.sync_run(
        db,
        now - timedelta(minutes=20),
        trigger="daemon",
        kinds=("map",),
        stages=(("map", 38600, 30.9),),
    )

    # Redis: the call budget, the flight quota, the version marker and the latencies.
    quota.consume_open_meteo_budget(340.0, now=now)
    quota.consume_open_meteo_budget(170.0, now=now)
    for _ in range(2):  # the Windy adapter's daily request counter (P1 providers)
        quota.consume_windy_request(now=now)
    monkeypatch.setattr(get_settings(), "flight_provider", "duffel")
    consume_quota("duffel", get_settings().flight_daily_quota, 7)
    versions.write_version_marker(versions.version_payload(v2))
    versions.note_first_served(int(v2.id), now=published_at + timedelta(seconds=4))
    for seconds in (12.0, 21.5, 33.0):
        versions.record_seen(
            int(v2.id), published_at + timedelta(seconds=seconds), published_at=published_at
        )

    # Flight searches and emails today (plus one search from yesterday, not counted). The
    # report counts searches since midnight UTC, so today's are placed between midnight and
    # now: "30 minutes ago" was yesterday in the first half hour of every day.
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    _flight(db, "duffel", FlightSearchStatus.SUCCESS, today + (now - today) * 0.25)
    _flight(db, "duffel", FlightSearchStatus.FAILED, today + (now - today) * 0.5)
    _flight(db, "demo", FlightSearchStatus.SUCCESS, today + (now - today) * 0.75)
    _flight(db, "duffel", FlightSearchStatus.SUCCESS, now - timedelta(days=1, hours=1))
    user = db.scalar(select(User).where(User.email == "ops@example.com"))
    assert user is not None
    db.add(
        EmailMessage(
            user_id=user.id,
            purpose=EmailPurpose.TEST,
            status=EmailDeliveryStatus.FAILED,
            recipient="ops@example.com",
            subject="synthetic",
        )
    )
    db.add(
        Notification(
            user_id=user.id,
            channel=NotificationChannel.EMAIL,
            kind=NotificationKind.NEW_OPPORTUNITY,
            status=NotificationStatus.ACCEPTED,
            dedup_key="synthetic-alert-1",
            recipient="ops@example.com",
            subject="synthetic",
            body_text="synthetic",
        )
    )
    db.commit()

    resp = client.get(PATH)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Provider and models.
    assert body["active_provider"]["primary"] == "open_meteo"
    assert body["active_provider"]["fallback_active"] is False
    assert body["version_id"] == v2.id
    wave, wind = body["wave"], body["atmosphere"]
    assert wave["model"] == "ncep_gfswave025" and wave["changed"] is True
    assert wave["run"].startswith(c2.strftime("%Y-%m-%dT%H:00"))
    assert wind["model"] == "ncep_gfs013,ncep_gfs025"
    assert wind["changed"] is False and wind["carried_from_run_id"] == run1.id
    assert wind["run"].startswith(c1.strftime("%Y-%m-%dT%H:00"))  # carried: its own cycle
    assert [d["model"] for d in wind["domains"]] == ["ncep_gfs013", "ncep_gfs025"]
    assert wave["typical_lag_minutes"] == 317.0 and wave["lag_samples"] == 2
    assert wind["typical_lag_minutes"] == 322.0 and wind["lag_samples"] == 1
    assert wave["update_interval_minutes"] == 360.0 and wave["update_interval_recorded"]
    assert wave["late"] is False and wave["expected_next_at"] is not None

    # Detection, acquisition, scoring, publication.
    assert body["last_detected_model_update"]["version_id"] == v2.id
    assert body["last_successful_acquisition"]["version_id"] == v2.id
    assert body["last_successful_scoring"]["version_id"] == v2.id
    published = body["last_published_version"]
    assert published["id"] == v2.id and published["provider"] == "open_meteo"
    assert published["counts"] == {
        "total": len(spots),
        "updated": len(spots) - 3,
        "stale": 3,
        "failed": 3,
    }
    assert body["spots_updated"] == len(spots) - 3

    # Stale spots: the three that failed, with their reason and error, by slug.
    stale = {s["spot_id"]: s for s in body["stale_spots"]}
    assert set(stale) == set(failed_spots)
    slugs = dict(db.execute(select(SurfSpot.id, SurfSpot.slug)).all())
    assert {s["slug"] for s in stale.values()} == {slugs[i] for i in failed_spots}
    assert all(s["reason"] == "refresh_failed" for s in stale.values())
    assert stale[spots[1]]["error"] == "HTTP 500"

    # Failed requests (24 h) and API usage against the limits.
    assert body["failed_requests"] == [
        {"provider": "open_meteo_noaa", "status": "500", "count": 2},
        {"provider": "duffel", "status": "failed", "count": 1},
        {"provider": "open_meteo_noaa", "status": "429", "count": 1},
    ]
    usage = body["api_usage"]
    assert usage["open_meteo"]["day"]["used"] == 510.0
    assert usage["open_meteo"]["day"]["limit"] == 10000.0
    assert usage["open_meteo"]["day"]["fraction"] == 0.051
    assert usage["open_meteo"]["month"]["used"] == 510.0 and usage["open_meteo"]["available"]
    # The Windy meter reads the adapter's own counter now that the providers package is
    # merged (it pinned "not counted" while monitoring was built without it).
    assert usage["windy"]["counted"] is True and usage["windy"]["available"] is True
    assert usage["windy"]["day"]["used"] == 2.0
    assert usage["windy"]["day"]["limit"] == float(get_settings().windy_daily_request_limit)
    assert usage["flights"]["today"] == 3
    assert usage["flights"]["per_provider"] == {"demo": 1, "duffel": 2}
    assert usage["flights"]["failed_today"] == 1
    assert usage["flights"]["quota"]["used"] == 7.0 and usage["flights"]["quota"]["limit"] == 300.0
    assert usage["emails"] == {
        "provider": "console",
        "today": 3,
        "account": 2,
        "alerts": 1,
        "failed": 1,
    }
    requests = {r["source"]: r for r in usage["requests_24h"]}
    om = requests["open_meteo_noaa"]
    assert om["runs"] == 3 and om["failed_runs"] == 1 and om["requests"] == 9
    assert om["weighted_calls"] == 510.0 and om["failed_requests"] == 3
    assert om["http_status_counts"] == {"200": 6, "429": 1, "500": 2}

    # Processing times: the three spot refreshes (the map-only sync is not one).
    processing = body["processing_time"]
    assert processing["refreshes"] == 3
    stages = {s["stage"]: s for s in processing["stages"]}
    assert stages["predict"]["runs"] == 2 and stages["predict"]["avg_ms"] == 8950.0
    assert stages["spots"]["runs"] == 3 and stages["spots"]["max_ms"] == 4100.0
    assert "map" not in stages
    assert processing["scored_to_published_s"]["p50"] == 2.0
    assert processing["acquired_to_scored_s"]["samples"] == 2

    # Latencies: availability -> publication per version, publication -> browsers.
    latency = body["latency"]
    up = latency["upstream_to_publication_s"]
    assert up["samples"] == 2 and up["last"] == pytest.approx(12 * 60 + 25)
    assert latency["publication_to_frontend_s"] == {
        "samples": 3,
        "last": 33.0,
        "avg": pytest.approx(22.167, abs=1e-3),
        "p50": 21.5,
        "p95": 33.0,
        "max": 33.0,
    }
    assert latency["first_served_s"]["samples"] == 1 and latency["first_served_s"]["last"] == 4.0

    # Refresh jobs, the freshness timeline, the scheduler and the problems.
    jobs = {j["id"]: j for j in body["refresh_jobs"]}
    assert len(jobs) == 4
    assert jobs[sync2.id]["version_ids"] == [v2.id] and jobs[sync1.id]["version_ids"] == [v1.id]
    assert jobs[sync3.id]["status"] == "failed" and "HTTP 500" in jobs[sync3.id]["error"]
    assert jobs[map_sync.id]["refresh"] is False and jobs[map_sync.id]["kinds"] == ["map"]
    assert all(jobs[s.id]["refresh"] for s in (sync1, sync2, sync3))
    assert jobs[sync2.id]["stages"][1] == {
        "name": "predict",
        "status": "success",
        "duration_ms": 8950,
        "cpu_s": 7.18,
    }
    assert [v["id"] for v in body["versions"]] == [v2.id, v1.id]
    assert body["versions"][0]["changed"] == ["wave"]
    assert body["versions"][1]["changed"] == ["atmosphere", "wave"]
    assert body["versions"][0]["status"] == "published"
    assert body["versions"][1]["status"] == "superseded"
    scheduler = body["scheduler"]
    assert scheduler["enabled"] and not scheduler["silent"]
    assert scheduler["last_check_by"] == f"sync {sync3.id} (http)"
    assert scheduler["expected_minutes"] == 30
    # The failed atmosphere attempt saw the c2 cycle: it is not late, it failed to refresh.
    assert wind["newest_run"].startswith(c2.strftime("%Y-%m-%dT%H:00")) and not wind["late"]
    keys = {p["key"] for p in body["problems"]}
    assert "forecast_stale_spots" in keys
    assert not {
        k for k in keys if k.startswith(("model_run_late", "quota_near", "refresh_failures"))
    }
    stale_problem = next(p for p in body["problems"] if p["key"] == "forecast_stale_spots")
    assert "3 of" in stale_problem["message"] and "HTTP 500" in stale_problem["detail"]
    assert body["status"] in ("degraded", "failing")
