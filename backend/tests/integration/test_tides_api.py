"""Tide acquisition, storage, the tide block and its endpoint (real PostgreSQL).

CO-OPS answers are served through httpx.MockTransport. The high and low waters are the
REAL Pillar Point predictions of tests/fixtures/coops/hilo14_9414131.json (2026-10-14 to
2026-10-27, MLLW); the test serves them for every CO-OPS station it asks for, so heights at
the other stations are stand-ins. The 30-minute curve of the harmonic station is SYNTHETIC
(the cosine interpolation of those events), labelled as such here. Model tides are the
real EOT20 synthesis (no network).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import SpotTideStation, SurfSpot, TidePrediction, TideStation
from app.schemas.tides import TIDE_PREFERENCE_NOT_ESTABLISHED, ObservationsBlockOut, TideBlockOut
from app.services.research.assess import seed_research
from app.services.seed import resolve_data_dir
from app.services.tides import api as tide_api
from app.services.tides import coops
from app.services.tides.catalog import seed_tide_catalog
from app.services.tides.health import tide_freshness
from app.services.tides.ingest import acquire_tides
from app.services.tides.maintenance import prune_tide_predictions
from app.services.tides.stage import tide_stages_for
from tests.conftest import ApiClient

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "coops"
REPO = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
ACTIVE = ["mavericks", "ocean-beach-sf", "pipeline", "nazare", "cloudbreak"]


def _json(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def _hilo_body() -> dict[str, object]:
    return _json("hilo14_9414131.json")  # type: ignore[return-value]


def _synthetic_curve_body() -> dict[str, object]:
    """SYNTHETIC 30-minute curve: cosine interpolation of the real events."""
    events = coops.parse_predictions(_hilo_body(), "MLLW")
    curve = coops.cosine_curve(events, timedelta(minutes=30), datetime(2026, 10, 14, tzinfo=UTC))
    return {
        "predictions": [
            {"t": p.t.strftime("%Y-%m-%d %H:%M"), "v": f"{p.height_m:.3f}"} for p in curve
        ]
    }


def coops_transport(
    calls: list[str], fail: Callable[[httpx.Request], httpx.Response | None] | None = None
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            f"{request.url.path}?{request.url.params.get('station', '')}"
            f"&{request.url.params.get('interval', '')}"
        )
        if fail is not None and (answer := fail(request)) is not None:
            return answer
        path = request.url.path
        if path == "/mdapi/prod/webapi/stations.json":
            return httpx.Response(200, json=_json("stations_tidepredictions.trimmed.json"))
        if path.endswith("/datums.json"):
            if "/9414131/" in path:
                return httpx.Response(200, json=_json("datums_9414131.json"))
            # Shape of a station without datums (9414275 on 2026-10-15).
            return httpx.Response(200, json={"units": "meters", "datums": None})
        if path == "/api/prod/datagetter":
            assert request.url.params["time_zone"] == "gmt"
            assert request.url.params["units"] == "metric"
            assert request.url.params["datum"] == "MLLW"
            if request.url.params["interval"] == "hilo":
                return httpx.Response(200, json=_hilo_body())
            return httpx.Response(200, json=_synthetic_curve_body())
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def tide_spots(db: Session) -> list[str]:
    db.execute(text("UPDATE surf_spots SET is_active = (slug = ANY(:keep))"), {"keep": ACTIVE})
    db.commit()
    coops.clear_station_cache()
    return ACTIVE


def _spot(db: Session, slug: str) -> SurfSpot:
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug))
    assert spot is not None
    return spot


def _rows(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(TidePrediction)) or 0)


def _block(client: ApiClient, slug: str, start: str, end: str) -> TideBlockOut:
    resp = client.get(f"/api/spots/{slug}/tides", params={"start": start, "end": end})
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "public, max-age=300"
    return TideBlockOut.model_validate(resp.json())


def test_tides_are_unavailable_rather_than_invented(app_client: ApiClient, db: Session) -> None:
    """No catalog seeded, no forecast run: nothing is made up."""
    resp = app_client.get("/api/spots/mavericks/tides")
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "public, max-age=300"
    block = TideBlockOut.model_validate(resp.json())
    assert block.status == "unavailable"
    assert block.reason == "No tide source configured for this spot."
    assert block.curve == [] and block.events == [] and block.station is None
    assert block.datum is None and block.source is None and block.units == "m"
    # The preference still follows the research rule (see the preference test below); for
    # Mavericks (tide research low, decision disputed) it is never established.
    assert block.preference == tide_api.tide_preference(db, _spot(db, "mavericks"))
    assert block.preference.text == TIDE_PREFERENCE_NOT_ESTABLISHED

    window = app_client.get(
        "/api/spots/mavericks/tides",
        params={"start": "2026-10-14T00:00:00Z", "end": "2026-10-16T00:00:00Z"},
    )
    assert window.status_code == 200 and window.json()["status"] == "unavailable"
    backwards = app_client.get(
        "/api/spots/mavericks/tides",
        params={"start": "2026-10-16T00:00:00Z", "end": "2026-10-14T00:00:00Z"},
    )
    assert backwards.status_code == 400
    too_long = app_client.get(
        "/api/spots/mavericks/tides",
        params={"start": "2026-10-01T00:00:00Z", "end": "2026-11-02T00:00:00Z"},
    )
    assert too_long.status_code == 400 and "31 days" in too_long.json()["error"]["message"]


def test_observations_are_unavailable_rather_than_invented(app_client: ApiClient) -> None:
    resp = app_client.get("/api/spots/rincon/observations")
    assert resp.status_code == 200, resp.text
    block = ObservationsBlockOut.model_validate(resp.json())
    assert block.status == "unavailable" and block.items == [] and block.reason


@pytest.mark.parametrize("path", ["tides", "observations"])
def test_unknown_spot_is_404(app_client: ApiClient, path: str) -> None:
    resp = app_client.get(f"/api/spots/no-such-spot/{path}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_catalog_seed_is_idempotent(db: Session) -> None:
    first = seed_tide_catalog(db)
    db.commit()
    assert first["spot_links"] == 58  # 50 primary + 8 US fallbacks
    second = seed_tide_catalog(db)
    db.commit()
    assert second["created"] == 0 and second["spot_links"] == 58
    assert db.scalar(select(func.count()).select_from(SpotTideStation)) == 58
    pillar = db.scalar(select(TideStation).where(TideStation.code == "9414131"))
    assert pillar is not None and pillar.kind == "harmonic" and pillar.provider == "noaa_coops"
    model = db.scalar(select(TideStation).where(TideStation.code == "eot20:nazare"))
    assert model is not None and model.kind == "model"
    assert model.attribution == "EOT20, Hart-Davis et al. 2021, DGFI-TUM/SEANOE"
    assert model.licence == "CC BY 4.0"


def test_acquire_stores_coops_and_model_tides_idempotently(
    app_client: ApiClient, db: Session, tide_spots: list[str]
) -> None:
    calls: list[str] = []
    out = acquire_tides(db, NOW, client=coops_transport(calls), backoff_seconds=0)
    assert out["status"] == "ok", out
    assert out["window"] == {
        "start": "2026-10-13T00:00:00+00:00",
        "end": "2026-10-31T13:00:00+00:00",
    }
    co = out["providers"]["noaa_coops"]
    assert set(co["stations"]) == {"9414131", "9414275", "1612668"}
    # Station list once, datums and high/low waters per station, a curve only for the
    # harmonic station (subordinate curves are interpolated here).
    assert co["requests"] == 1 + 3 + 3 + 1
    assert co["stations"]["9414131"]["interpolated"] is False
    assert co["stations"]["9414275"]["interpolated"] is True
    model = out["providers"]["harmonic_model"]["stations"]
    assert set(model) == {f"eot20:{s}" for s in ACTIVE}
    stored = _rows(db)

    # The same day again: nothing new, nothing removed, no station check.
    calls.clear()
    again = acquire_tides(db, NOW, client=coops_transport(calls), backoff_seconds=0)
    assert again["status"] == "ok"
    assert again["providers"]["noaa_coops"]["requests"] == 3 + 1
    for provider in ("noaa_coops", "harmonic_model"):
        for entry in again["providers"][provider]["stations"].values():
            assert entry["inserted"] == 0 and entry["removed"] == 0, entry
    assert _rows(db) == stored

    # A harmonic CO-OPS station: its own curve and events, MLLW, with the MSL offset.
    block = _block(app_client, "mavericks", "2026-10-15T00:00:00Z", "2026-10-17T00:00:00Z")
    assert block.status == "available" and block.source == "noaa_coops"
    assert block.datum == "MLLW" and block.units == "m"
    assert block.station is not None
    assert (block.station.code, block.station.kind, block.station.distance_km) == (
        "9414131",
        "harmonic",
        2.0,
    )
    assert "0.91 m above MLLW" in (block.reason or "")
    assert block.attribution and "CO-OPS" in block.attribution
    assert not any(p.interpolated for p in block.curve)
    gaps = {b.t - a.t for a, b in zip(block.curve, block.curve[1:], strict=False)}
    assert gaps == {timedelta(minutes=30)}
    start, end = datetime(2026, 10, 15, tzinfo=UTC), datetime(2026, 10, 17, tzinfo=UTC)
    assert block.curve[0].t <= start and block.curve[-1].t >= end
    # Events in the window plus one on each side; they alternate.
    assert block.events[0].t < start < block.events[1].t
    assert block.events[-2].t < end < block.events[-1].t
    kinds = [e.kind for e in block.events]
    assert all(a != b for a, b in zip(kinds, kinds[1:], strict=False))
    assert block.fetched_at == NOW

    # A subordinate station: the curve is flagged interpolated and the note says so.
    ob = _block(app_client, "ocean-beach-sf", "2026-10-15T00:00:00Z", "2026-10-16T00:00:00Z")
    assert ob.status == "available" and ob.station is not None
    assert ob.station.kind == "subordinate" and ob.datum == "MLLW"
    assert ob.curve and all(p.interpolated for p in ob.curve)
    assert "interpolated" in (ob.reason or "")

    # A model point: datum model_msl, attribution, the catalog's caveat where there is one.
    naz = _block(app_client, "nazare", "2026-10-15T00:00:00Z", "2026-10-16T00:00:00Z")
    assert naz.status == "available" and naz.source == "eot20" and naz.datum == "model_msl"
    assert naz.station is not None and naz.station.kind == "model"
    assert naz.station.code == "eot20:nazare" and naz.station.distance_km == 3.8
    assert naz.reason == tide_api.MODEL_NOTE
    assert naz.attribution == "EOT20, Hart-Davis et al. 2021, DGFI-TUM/SEANOE (CC BY 4.0)"
    assert 6 <= len(naz.events) <= 8
    reef = _block(app_client, "cloudbreak", "2026-10-15T00:00:00Z", "2026-10-16T00:00:00Z")
    assert reef.reason is not None and reef.reason.startswith(tide_api.MODEL_NOTE)
    assert "reef" in reef.reason

    # Outside every stored prediction: unavailable, with the reason.
    later = _block(app_client, "nazare", "2026-12-01T00:00:00Z", "2026-12-02T00:00:00Z")
    assert later.status == "unavailable" and later.reason == tide_api.NOT_STORED


def test_a_failed_coops_station_falls_back_to_the_model_without_mixing(
    app_client: ApiClient, db: Session, tide_spots: list[str]
) -> None:
    error = _json("h_1612668_error.json")

    def fail(request: httpx.Request) -> httpx.Response | None:
        if request.url.params.get("station") == "1612668":
            return httpx.Response(200, json=error)  # CO-OPS's HTTP-200 error body
        return None

    calls: list[str] = []
    out = acquire_tides(db, NOW, client=coops_transport(calls, fail), backoff_seconds=0)
    assert out["status"] == "partial"
    entry = out["providers"]["noaa_coops"]["stations"]["1612668"]
    assert "No Predictions data was found" in entry["error"] and entry["transient"] is False
    station = db.scalar(select(TideStation).where(TideStation.code == "1612668"))
    assert station is not None
    db.refresh(station)
    assert "No Predictions" in station.metadata_["last_error"]
    assert (
        db.scalar(
            select(func.count())
            .select_from(TidePrediction)
            .where(TidePrediction.station_id == station.id)
        )
        == 0
    )
    block = _block(app_client, "pipeline", "2026-10-15T00:00:00Z", "2026-10-16T00:00:00Z")
    assert block.status == "available" and block.source == "eot20"
    assert block.datum == "model_msl" and block.station is not None
    assert block.station.code == "eot20:pipeline"
    # The health check reports the station that has no predictions.
    problems = tide_freshness(db, NOW)
    assert [p["key"] for p in problems] == ["tides_stale:noaa_coops"]
    assert problems[0]["severity"] == "degraded" and "1612668" in problems[0]["detail"]


def test_a_moved_high_or_low_water_replaces_the_stored_one(
    db: Session, tide_spots: list[str]
) -> None:
    """A re-run whose answer moved an event by a minute stores the new time and deletes
    the old row, so the station never shows two versions of one high or low water."""
    acquire_tides(db, NOW, client=coops_transport([]), backoff_seconds=0)
    rows = json.loads((FIXTURES / "hilo14_9414131.json").read_text())["predictions"]
    old_t = datetime.strptime(rows[5]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    new_t = old_t + timedelta(minutes=1)
    rows[5]["t"] = new_t.strftime("%Y-%m-%d %H:%M")

    def moved(request: httpx.Request) -> httpx.Response | None:
        if request.url.params.get("interval") == "hilo":
            return httpx.Response(200, json={"predictions": rows})
        return None

    out = acquire_tides(db, NOW, client=coops_transport([], moved), backoff_seconds=0)
    entry = out["providers"]["noaa_coops"]["stations"]["9414131"]
    assert (entry["inserted"], entry["removed"]) == (1, 1), entry
    station = db.scalar(select(TideStation).where(TideStation.code == "9414131"))
    assert station is not None
    events = db.scalars(
        select(TidePrediction.valid_time).where(
            TidePrediction.station_id == station.id, TidePrediction.is_event
        )
    ).all()
    assert new_t in events and old_t not in events
    assert len(events) == len(set(events)) == len(rows)


def test_a_wrong_catalog_entry_costs_only_that_spot(
    app_client: ApiClient, db: Session, tmp_path: Path
) -> None:
    catalog = json.loads((REPO / "data" / "tide_stations.json").read_text())
    catalog["spots"]["rincon"]["stations"][0]["distance_km"] = 40.0  # wrong position
    path = tmp_path / "tide_stations.json"
    path.write_text(json.dumps(catalog))
    out = seed_tide_catalog(db, path)
    db.commit()
    assert set(out["rejected_spots"]) == {"rincon"}
    assert "distance is 4.3 km, not 40.0" in out["rejected_spots"]["rincon"]
    assert out["spot_links"] == 56  # 58 less Rincon's two
    rincon = _spot(db, "rincon")
    linked = db.scalar(
        select(func.count())
        .select_from(SpotTideStation)
        .where(SpotTideStation.spot_id == rincon.id)
    )
    assert linked == 0
    block = _block(app_client, "rincon", "2026-10-15T00:00:00Z", "2026-10-16T00:00:00Z")
    assert block.status == "unavailable" and block.station is None and block.curve == []


def test_a_missing_constants_file_is_reported(
    db: Session, tide_spots: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the constants file no model point is linked: the job and the health check
    say so instead of the model spots' tides vanishing quietly."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "tide_constants_path", str(tmp_path / "none.json"))
    out = acquire_tides(db, NOW, client=coops_transport([]), backoff_seconds=0)
    assert out["status"] == "partial"
    assert out["catalog"]["spot_links"] == 8  # the CO-OPS links of the 8 US spots only
    assert "constants unavailable" in out["providers"]["harmonic_model"]["error"]
    problems = {p["key"]: p["severity"] for p in tide_freshness(db, NOW)}
    assert problems == {"tides_constants:harmonic_model": "failing"}


def test_tide_stages_and_pruning(db: Session, tide_spots: list[str]) -> None:
    acquire_tides(db, NOW, client=coops_transport([]), backoff_seconds=0)
    mavericks = _spot(db, "mavericks")
    events = coops.parse_predictions(_hilo_body(), "MLLW")
    inside = [e for e in events if datetime(2026, 10, 15, tzinfo=UTC) <= e.t]
    times = [e.t for e in inside[:6]] + [datetime(2026, 10, 15, 6, tzinfo=UTC)]
    stages = tide_stages_for(db, mavericks.id, times)
    for e in inside[:6]:
        assert stages[e.t].stage == (1.0 if e.event == "high" else 0.0)
        assert stages[e.t].source == "noaa_coops"
    assert 0.0 <= stages[times[-1]].stage <= 1.0
    # A model spot gets its stages from the EOT20 predictions.
    nazare = _spot(db, "nazare")
    hourly = [datetime(2026, 10, 15, tzinfo=UTC) + timedelta(hours=h) for h in range(24)]
    model_stages = tide_stages_for(db, nazare.id, hourly)
    assert len(model_stages) == 24 and {s.source for s in model_stages.values()} == {"eot20"}
    assert {s.rising for s in model_stages.values()} == {True, False}
    assert all(s.range_m > 1.0 for s in model_stages.values())  # Nazare: metres of tide

    # Pruning keeps the future and TIDE_RETENTION_PAST_DAYS (2) of the past.
    later = datetime(2026, 10, 20, tzinfo=UTC)
    cutoff = later - timedelta(days=2)
    old = db.scalar(
        select(func.count()).select_from(TidePrediction).where(TidePrediction.valid_time < cutoff)
    )
    total = _rows(db)
    deleted = prune_tide_predictions(db, later)
    db.commit()
    assert deleted == old and deleted > 0 and _rows(db) == total - deleted
    assert db.scalar(select(func.min(TidePrediction.valid_time))) >= cutoff


def test_freshness_degrades_then_fails_as_predictions_run_out(
    db: Session, tide_spots: list[str]
) -> None:
    assert tide_freshness(db, NOW) == []  # nothing linked yet: the job check covers it
    acquire_tides(db, NOW, client=coops_transport([]), backoff_seconds=0)
    assert tide_freshness(db, NOW) == []
    # The CO-OPS events end 2026-10-27 18:14 (fixture); the model runs to 2026-10-31.
    soon = tide_freshness(db, datetime(2026, 10, 26, tzinfo=UTC))
    assert [(p["key"], p["severity"]) for p in soon] == [("tides_stale:noaa_coops", "degraded")]
    assert "run out within 2 days" in soon[0]["message"]
    gone = {p["key"]: p["severity"] for p in tide_freshness(db, datetime(2026, 11, 2, tzinfo=UTC))}
    assert gone == {"tides_stale:noaa_coops": "failing", "tides_stale:harmonic_model": "failing"}


def test_the_preference_text_follows_the_research(db: Session) -> None:
    seed_research(db, resolve_data_dir(None))  # the assessments (truncated between tests)
    db.commit()
    established = not_established = 0
    for spot in db.scalars(select(SurfSpot)).all():
        pref = tide_api.tide_preference(db, spot)
        assert pref.value == (spot.tide_preference.value if spot.tide_preference else None)
        ok = (
            pref.value is not None
            and pref.decision != "disputed"
            and pref.research_class in ("moderate", "high")
        )
        if ok:
            established += 1
            assert pref.text == tide_api.PREFERENCE_TEXT[str(pref.value)]
        else:
            not_established += 1
            assert pref.text == TIDE_PREFERENCE_NOT_ESTABLISHED
    # On the committed research (2026-10-15) 18 of 50 preferences are shown (10 of them
    # other than "all"); e.g. Mundaka (tide research high, no decision record) is shown,
    # Anchor Point (decision "disputed") is not.
    assert established > 0 and not_established > 0
    mundaka = tide_api.tide_preference(db, _spot(db, "mundaka"))
    assert (mundaka.research_class, mundaka.text) == ("high", "Best from low to mid tide.")
    anchor = tide_api.tide_preference(db, _spot(db, "anchor-point"))
    assert anchor.decision == "disputed" and anchor.text == TIDE_PREFERENCE_NOT_ESTABLISHED


def test_demo_forecasts_get_a_labelled_synthetic_tide(
    app_client: ApiClient, db: Session, few_spots: list[str]
) -> None:
    from app.services.pipeline import run_pipeline

    run_pipeline(db)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start, end = now + timedelta(hours=1), now + timedelta(hours=49)
    params = {"start": start.isoformat(), "end": end.isoformat()}
    resp = app_client.get("/api/spots/nazare/tides", params=params)
    block = TideBlockOut.model_validate(resp.json())
    assert block.status == "demo" and block.source == "demo"
    assert block.datum is None and block.station is None
    assert block.reason == tide_api.DEMO_REASON and block.attribution == "Synthetic demo data"
    assert len(block.curve) >= 49 and block.events
    # Real stored predictions win over the demo provider's synthetic sea level.
    seed_tide_catalog(db)
    db.commit()
    acquire_tides(db, now, client=coops_transport([]), backoff_seconds=0)
    real = TideBlockOut.model_validate(
        app_client.get("/api/spots/nazare/tides", params=params).json()
    )
    assert real.status == "available" and real.source == "eot20"


def test_cli_seed_and_list(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    from app.cli import main

    assert main(["tides", "seed"]) == 0
    seeded = json.loads(capsys.readouterr().out)
    assert seeded["spot_links"] == 58 and seeded["tide_stations"] == 56  # 6 CO-OPS + 50 cells
    assert main(["tides", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    pillar = next(s for s in listed if s["code"] == "9414131")
    assert pillar["spots"] == ["mavericks (primary)"] and pillar["predictions"] == 0
    assert main(["tides", "prune"]) == 0
    assert json.loads(capsys.readouterr().out) == {"tide_predictions_deleted": 0}


def test_a_tide_model_setting_without_its_constants_is_reported(
    db: Session, tide_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "tide_model", "fes2022")
    monkeypatch.setattr(get_settings(), "tide_providers", "harmonic_model")
    out = acquire_tides(db, NOW)
    model = out["providers"]["harmonic_model"]
    assert "fes2022" in model["warnings"][0] and "'eot20'" in model["warnings"][0]
    assert {e["events"] > 0 for e in model["stations"].values()} == {True}
    sources = db.scalars(select(TidePrediction.source_code).distinct()).all()
    assert sources == ["eot20"]  # labelled by what was computed
