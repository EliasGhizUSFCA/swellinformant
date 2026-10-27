"""Storage bounds: the archive's confidence digest, pruning the predictions of superseded
member runs, the retention of quality-controlled observations and of sync_runs rows.

Every forecast value and observation here is SYNTHETIC test data written by the test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    BuoyObservation,
    BuoyStation,
    ForecastRun,
    PredictionArchive,
    SurfQualityPrediction,
    SyncRun,
)
from app.services.forecasts import blend, queries
from app.services.forecasts.archive import confidence_digest
from app.services.forecasts.base import (
    FetchResult,
    ForecastProvider,
    ForecastRecord,
    RunInfo,
    SourceInfo,
    SpotPoint,
)
from app.services.forecasts.ingestion import acquire_from_provider
from app.services.maintenance import cleanup, prune_superseded_predictions
from app.services.surf_quality.predict import compute_pending_predictions

pytestmark = pytest.mark.integration

@pytest.fixture(autouse=True)
def _prediction_archive_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prediction archive is optional and off by default since Swell 2.0
    (PREDICTION_ARCHIVE_ENABLED); these tests check what it archives, so it is on."""
    monkeypatch.setattr(get_settings(), "prediction_archive_enabled", True)


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


class SyntheticProvider(ForecastProvider):
    """A constant SYNTHETIC sea state, 3-hourly for 48 h, for some or all active spots."""

    def __init__(
        self, code: str, family: str, hs: float, issued: datetime, only: set[str] | None = None
    ) -> None:
        self.code, self.family, self.hs, self.issued, self.only = code, family, hs, issued, only

    def source_info(self) -> SourceInfo:
        return SourceInfo(
            self.code, self.code, "test", "w", "a", False, f"{self.code} data", "test", self.family
        )

    def current_run(self, now: datetime) -> RunInfo:
        return RunInfo(f"{self.code}:{self.issued:%Y%m%d%H}", self.issued, self.issued)

    def fetch(self, spots: list[SpotPoint], run: RunInfo) -> FetchResult:
        out = FetchResult()
        for p in spots:
            if self.only is not None and p.slug not in self.only:
                continue
            out.records[p.spot_id] = [
                ForecastRecord(
                    run.issued_at + timedelta(hours=h),
                    {
                        "sig_wave_height_m": self.hs,
                        "wave_period_s": 14.0,
                        "wave_direction_deg": 215.0,
                        "primary_swell_height_m": self.hs * 0.9,
                        "primary_swell_period_s": 14.0,
                        "primary_swell_direction_deg": 215.0,
                        "wind_speed_kmh": 10.0,
                        "wind_direction_deg": 290.0,
                    },
                    period_kind="tp",
                )
                for h in range(0, 48, 3)
            ]
        return out


@pytest.fixture
def codes(db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    configured = ["t_gfs", "t_ecmwf"]
    monkeypatch.setattr(blend, "configured_source_codes", lambda: configured)
    monkeypatch.setattr(queries, "configured_source_codes", lambda: configured)
    return configured


def _ingest(db: Session, provider: SyntheticProvider, priority: int = 0) -> ForecastRun:
    out = acquire_from_provider(db, provider, now=NOW, priority=priority)
    assert out.status in ("success", "partial"), out
    run = db.get(ForecastRun, out.run_id)
    assert run is not None
    return run


def _predictions(db: Session, run: ForecastRun) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(SurfQualityPrediction)
            .where(SurfQualityPrediction.run_id == run.id)
        )
        or 0
    )


def test_archive_rows_store_the_confidence_digest(db: Session, codes: list[str]) -> None:
    assert get_settings().archive_confidence_digest is True  # the default
    _ingest(db, SyntheticProvider("t_gfs", "ncep_gfs", 2.0, NOW - timedelta(hours=6)))
    _ingest(db, SyntheticProvider("t_ecmwf", "ecmwf_ifs", 2.6, NOW - timedelta(hours=6)), 1)
    assert blend.build_blend(db, NOW).status == "success"
    assert compute_pending_predictions(db, NOW)["archived"] > 0

    row = db.scalars(select(PredictionArchive).limit(1)).one()
    pred = db.scalars(
        select(SurfQualityPrediction).where(
            SurfQualityPrediction.spot_id == row.spot_id,
            SurfQualityPrediction.valid_time == row.valid_time,
            SurfQualityPrediction.run_id
            == select(ForecastRun.id).where(ForecastRun.run_key == row.run_key).scalar_subquery(),
        )
    ).one()
    assert row.confidence_inputs == confidence_digest(pred.confidence_inputs)
    assert row.confidence_inputs is not None and row.confidence_inputs["digest"] == 1
    assert "reasons" not in row.confidence_inputs and "rules" not in row.confidence_inputs
    full, digest, row_bytes = db.execute(
        text(
            "SELECT avg(pg_column_size(p.confidence_inputs)), avg(pg_column_size(a.confidence_inputs)),"
            " avg(pg_column_size(a.*)) FROM prediction_archive a"
            " JOIN forecast_runs r ON r.run_key = a.run_key"
            " JOIN surf_quality_predictions p ON p.run_id = r.id AND p.spot_id = a.spot_id"
            " AND p.valid_time = a.valid_time"
        )
    ).one()
    assert float(digest) < 0.3 * float(full)  # the reason sentences are not archived
    # The row with the full inputs would be row_bytes - digest + full. Measured on the real
    # 2026-10-11 blend: 562 B instead of 1,506 B (0.37); about 508 B on this fixture. The
    # audit's estimate of < 400 B would need dropping the disagreement values and classes
    # the digest keeps (about 280 B of every row are fixed columns).
    full_row = float(row_bytes) - float(digest) + float(full)
    assert float(row_bytes) < 0.5 * full_row, (row_bytes, full_row)


def test_digest_keeps_the_rules_and_inputs_a_recalibration_needs() -> None:
    inputs = {  # the shape forecast_confidence.confidence_inputs stores (values SYNTHETIC)
        "method": "forecast-v1",
        "class": "low",
        "rules": ["note:lead_3_7d", "cap:low:hs_strong_disagree", "downgrade:spread_large"],
        "reasons": ["GFS and ECMWF disagree strongly on swell size."],
        "disagreement": {"hs_log_ratio": 1.1, "dir_diff_deg": 58.0, "tp_diff_s": 1.0},
        "ensemble": {"members": 31, "rel_spread": 0.02},
        "independent_families": 2,
        "local": {"assessment_id": 37, "reason": "Partly documented.", "variables": {}},
        "stale": False,
        "missing": [],
        "run_change_ratio": None,
    }
    assert confidence_digest(inputs) == {
        "digest": 1,
        "class": "low",
        "local": {"assessment_id": 37},
        "caps": ["low:hs_strong_disagree", "downgrade:spread_large"],
        "hs_lr": 1.1,
        "dir_d": 58.0,
        "tp_d": 1.0,
        "spread": 0.02,
        "fam": 2,
    }
    assert confidence_digest(None) is None


def test_cleanup_keeps_predictions_only_for_the_newest_run_per_source(
    db: Session, codes: list[str]
) -> None:
    old = _ingest(db, SyntheticProvider("t_gfs", "ncep_gfs", 2.0, NOW - timedelta(hours=12)))
    new = _ingest(db, SyntheticProvider("t_gfs", "ncep_gfs", 2.1, NOW - timedelta(hours=6)))
    ec = _ingest(db, SyntheticProvider("t_ecmwf", "ecmwf_ifs", 2.6, NOW - timedelta(hours=6)), 1)
    assert blend.build_blend(db, NOW).status == "success"
    compute_pending_predictions(db, NOW)
    blend_run = db.scalars(select(ForecastRun).where(ForecastRun.run_key.like("blend:%"))).one()
    assert all(_predictions(db, r) > 0 for r in (old, new, ec, blend_run))
    superseded = _predictions(db, old)

    out = cleanup(db, NOW)
    assert out["superseded_predictions"] == superseded
    db.expire_all()
    assert _predictions(db, old) == 0  # superseded member run: derived data pruned
    assert all(_predictions(db, r) > 0 for r in (new, ec, blend_run))
    assert "predictions_pruned_at" in (db.get(ForecastRun, old.id).details or {})  # type: ignore[union-attr]
    assert db.get(ForecastRun, old.id) is not None  # the run and its forecasts stay
    assert prune_superseded_predictions(db, NOW) == 0  # idempotent


def test_a_superseded_run_still_authoritative_for_a_spot_keeps_its_predictions(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(queries, "configured_source_codes", lambda: ["t_gfs"])
    monkeypatch.setattr(get_settings(), "forecast_blend_enabled", False)
    old = _ingest(db, SyntheticProvider("t_gfs", "ncep_gfs", 2.0, NOW - timedelta(hours=12)))
    partial = SyntheticProvider(
        "t_gfs", "ncep_gfs", 2.1, NOW - timedelta(hours=6), only={"pipeline", "nazare"}
    )
    new = _ingest(db, partial)
    compute_pending_predictions(db, NOW)
    chosen = queries.latest_runs_by_spot(db, NOW)
    assert {r.id for r in chosen.values()} == {old.id, new.id}  # old serves 3 of 5 spots
    assert prune_superseded_predictions(db, NOW) == 0
    assert _predictions(db, old) > 0


def test_a_pruned_run_never_covers_a_spot_until_it_is_scored_again(
    db: Session, codes: list[str]
) -> None:
    """The blend covered every spot, so the older GFS run was pruned. Later the blend is no
    longer current and the newest GFS run covers 2 spots: the other 3 fall back to the
    ECMWF run (which has predictions), not to the pruned GFS run (which has none)."""
    old = _ingest(db, SyntheticProvider("t_gfs", "ncep_gfs", 2.0, NOW - timedelta(hours=12)))
    two = {"pipeline", "nazare"}
    mid = _ingest(db, SyntheticProvider("t_gfs", "ncep_gfs", 2.1, NOW - timedelta(hours=9), two))
    ec = _ingest(db, SyntheticProvider("t_ecmwf", "ecmwf_ifs", 2.6, NOW - timedelta(hours=9)), 1)
    assert blend.build_blend(db, NOW).status == "success"
    compute_pending_predictions(db, NOW)
    assert prune_superseded_predictions(db, NOW) > 0
    db.commit()
    assert _predictions(db, old) == 0
    # A newer GFS run (scored), and no new blend yet: the blend's GFS member is superseded.
    new = _ingest(db, SyntheticProvider("t_gfs", "ncep_gfs", 2.2, NOW - timedelta(hours=6), two))
    compute_pending_predictions(db, NOW)
    chosen = {s: r.id for s, r in queries.latest_runs_by_spot(db, NOW).items()}
    assert set(chosen.values()) == {new.id, ec.id} and old.id not in chosen.values()
    assert mid.id not in chosen.values()  # its 2 spots are the newer run's
    # Scored again (e.g. predictions_computed_at reset by an operator), it serves again.
    old_run = db.get(ForecastRun, old.id)
    assert old_run is not None
    old_run.predictions_computed_at = None
    db.commit()
    compute_pending_predictions(db, NOW)
    assert _predictions(db, old) > 0
    assert old.id in {r.id for r in queries.latest_runs_by_spot(db, NOW).values()}


def test_quality_controlled_observations_follow_their_own_retention(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    station = BuoyStation(
        code="SYN1", provider="ndbc", name="synthetic station", latitude=0.0, longitude=0.0
    )
    db.add(station)
    db.flush()
    old = NOW - timedelta(days=800)  # older than RETENTION_ARCHIVE_DAYS (730)
    for i, flag in enumerate(("good", "probably_good", "not_qc", "bad")):
        db.add(
            BuoyObservation(
                station_id=station.id,
                observed_at=old + timedelta(hours=i),
                hs_m=1.0,
                qc_flag=flag,
                dataset=f"synthetic-{flag}",
            )
        )
    db.commit()
    settings = get_settings()
    assert settings.observation_retention_days == 3650 > settings.retention_archive_days
    out = cleanup(db, NOW)
    assert out["buoy_observations"] == 2
    kept = set(db.scalars(select(BuoyObservation.qc_flag)))
    assert kept == {"good", "probably_good"}  # QC'd history outlives the archive
    monkeypatch.setattr(settings, "observation_retention_days", 365)
    assert cleanup(db, NOW)["buoy_observations"] == 2
    assert db.scalar(select(func.count()).select_from(BuoyObservation)) == 0


def test_cleanup_keeps_sync_runs_for_the_job_log_retention_and_the_newest_publication(
    db: Session,
) -> None:
    """sync_runs rows (one per sync; SYNTHETIC rows here) follow RETENTION_JOB_LOG_DAYS,
    except the newest publication, which the map index reads, and a row still marked
    running, whose lease the next sync settles (it marks it abandoned)."""
    days = get_settings().retention_job_log_days
    old = NOW - timedelta(days=days + 1)
    recent = NOW - timedelta(days=days - 1)

    def row(started: datetime, status: str = "success", published: bool = False) -> SyncRun:
        r = SyncRun(
            trigger="daemon",
            status=status,
            started_at=started,
            published_at=started + timedelta(minutes=5) if published else None,
        )
        db.add(r)
        db.flush()
        return r

    old_published = row(old - timedelta(days=1), published=True)
    newest_published = row(old, published=True)  # old, but the newest publication
    old_plain = row(old, status="skipped")
    old_failed = row(old - timedelta(hours=1), status="failed")
    stuck = row(old, status="running")
    kept_recent = row(recent, status="partial")
    db.commit()

    out = cleanup(db, NOW)

    assert out["sync_runs"] == 3
    left = set(db.scalars(select(SyncRun.id)))
    assert left == {newest_published.id, stuck.id, kept_recent.id}
    assert not left & {old_published.id, old_plain.id, old_failed.id}
    # A newer publication makes the old one deletable at the next cleanup.
    row(recent, published=True)
    db.commit()
    assert cleanup(db, NOW)["sync_runs"] == 1
    assert newest_published.id not in set(db.scalars(select(SyncRun.id)))
    # Nothing published at all: plain age-based retention.
    db.execute(delete(SyncRun))
    row(old, status="skipped")
    db.commit()
    assert cleanup(db, NOW)["sync_runs"] == 1
