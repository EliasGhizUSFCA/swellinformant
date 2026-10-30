"""app.core.http_cache (foundation F6): browser and CDN cache headers; the version
endpoint's revalidated sharing and the no-store answer of a stale ``?v=`` (design D11)."""

from __future__ import annotations

import pytest
from fastapi import Response

from app.core.http_cache import (
    NO_STORE,
    PRIVATE_NO_STORE,
    cache_headers,
    no_store,
    shared_revalidated,
)


def headers(**kw: object) -> dict[str, str]:
    response = Response()
    cache_headers(response, **kw)  # type: ignore[arg-type]
    return {k: v for k, v in response.headers.items() if "cache-control" in k}


def test_browser_only() -> None:
    assert headers(browser_seconds=60) == {"cache-control": "public, max-age=60"}
    assert headers(browser_seconds=0) == {"cache-control": "public, max-age=0"}


def test_browser_and_cdn_with_stale_while_revalidate() -> None:
    assert headers(browser_seconds=60, cdn_seconds=60, stale_while_revalidate=600) == {
        "cache-control": "public, max-age=60",
        "cdn-cache-control": "public, s-maxage=60, stale-while-revalidate=600",
    }
    assert headers(browser_seconds=300, cdn_seconds=300) == {
        "cache-control": "public, max-age=300",
        "cdn-cache-control": "public, s-maxage=300",
    }


def test_immutable_run_keyed_urls() -> None:
    assert headers(browser_seconds=3600, cdn_seconds=31536000, immutable=True) == {
        "cache-control": "public, max-age=3600, immutable",
        "cdn-cache-control": "public, s-maxage=31536000, immutable",
    }
    assert headers(
        browser_seconds=3600, cdn_seconds=31536000, stale_while_revalidate=60, immutable=True
    ) == {
        "cache-control": "public, max-age=3600, immutable",
        "cdn-cache-control": "public, s-maxage=31536000, stale-while-revalidate=60, immutable",
    }


def test_an_existing_header_is_replaced() -> None:
    response = Response(headers={"Cache-Control": PRIVATE_NO_STORE})
    cache_headers(response, browser_seconds=60, cdn_seconds=60)
    assert response.headers.getlist("cache-control") == ["public, max-age=60"]
    assert response.headers.getlist("cdn-cache-control") == ["public, s-maxage=60"]


@pytest.mark.parametrize(
    "kw",
    [
        {"browser_seconds": -1},
        {"browser_seconds": 60, "cdn_seconds": -1},
        {"browser_seconds": 60, "cdn_seconds": 60, "stale_while_revalidate": -5},
    ],
)
def test_negative_lifetimes_are_refused(kw: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        cache_headers(Response(), **kw)  # type: ignore[arg-type]


def test_private_no_store_constant() -> None:
    assert PRIVATE_NO_STORE == "private, no-store"


def test_shared_revalidated_makes_browsers_ask_again_and_the_cdn_share_briefly() -> None:
    response = Response()
    shared_revalidated(response, cdn_seconds=15, stale_while_revalidate=15)
    assert {k: v for k, v in response.headers.items() if "cache-control" in k} == {
        "cache-control": "no-cache",
        "cdn-cache-control": "public, s-maxage=15, stale-while-revalidate=15",
    }
    response = Response()
    shared_revalidated(response, cdn_seconds=0)
    assert response.headers["cdn-cache-control"] == "public, s-maxage=0"
    with pytest.raises(ValueError):
        shared_revalidated(Response(), cdn_seconds=-1)
    with pytest.raises(ValueError):
        shared_revalidated(Response(), cdn_seconds=15, stale_while_revalidate=-1)


def test_no_store_replaces_both_the_browser_and_the_cdn_headers() -> None:
    response = Response()
    cache_headers(response, browser_seconds=60, cdn_seconds=300, stale_while_revalidate=3600)
    no_store(response)
    assert response.headers.getlist("cache-control") == [NO_STORE]
    assert response.headers.getlist("cdn-cache-control") == [NO_STORE]
    assert NO_STORE == "no-store"
