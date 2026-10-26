"""The public snapshot cache's key namespace per deployment (app.services.public_cache).

Production (VERCEL_ENV production or unset) keeps the namespace the daemon's marker is
stored in; a preview or development deployment gets its own, so production never serves
a payload computed by other code or from another database on a shared Redis. Deployment
ids below are SYNTHETIC.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services import public_cache

HOUR = datetime(2026, 10, 23, 23, tzinfo=UTC)


@pytest.mark.parametrize("environ", [{}, {"VERCEL_ENV": "production"}, {"VERCEL_ENV": ""}])
def test_production_and_no_vercel_env_share_the_namespace(environ: dict[str, str]) -> None:
    assert public_cache.namespace(environ | {"VERCEL_DEPLOYMENT_ID": "dpl_a"}) == "public:v1"


def test_a_preview_has_its_own_namespace_per_deployment() -> None:
    one = public_cache.namespace({"VERCEL_ENV": "preview", "VERCEL_DEPLOYMENT_ID": "dpl_one"})
    two = public_cache.namespace({"VERCEL_ENV": "preview", "VERCEL_DEPLOYMENT_ID": "dpl_two"})
    dev = public_cache.namespace({"VERCEL_ENV": "development", "VERCEL_URL": "localhost:3000"})
    assert one.startswith("public:v1:preview:") and two.startswith("public:v1:preview:")
    assert dev.startswith("public:v1:development:")
    assert len({one, two, dev, "public:v1"}) == 4
    assert "dpl_one" not in one  # a digest, not the id
    fallback = public_cache.namespace({"VERCEL_ENV": "preview", "VERCEL_URL": "app-x.vercel.app"})
    assert fallback.startswith("public:v1:preview:") and fallback != one


def test_cache_keys_and_the_marker_follow_the_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VERCEL_ENV", raising=False)
    production = public_cache.cache_key("e", "m", HOUR, "spots", "")
    assert production == "public:v1:e:m:20261023T23:spots"
    assert public_cache.marker_key() == public_cache.MARKER_KEY == "public:v1:marker"
    assert public_cache.epoch_key() == public_cache.EPOCH_KEY
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("VERCEL_DEPLOYMENT_ID", "dpl_synthetic")
    preview = public_cache.cache_key("e", "m", HOUR, "spots", "")
    assert preview != production and preview.endswith(":e:m:20261023T23:spots")
    assert public_cache.marker_key() != public_cache.MARKER_KEY
    assert public_cache.epoch_key() != public_cache.EPOCH_KEY
