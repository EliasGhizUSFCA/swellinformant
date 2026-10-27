"""Cache headers for responses that are the same for every visitor.

:func:`cache_headers` sets ``Cache-Control`` for browsers and, when ``cdn_seconds`` is given,
``CDN-Cache-Control`` for a shared CDN (Vercel's CDN reads it before Cache-Control:
Vercel-CDN-Cache-Control > CDN-Cache-Control > Cache-Control, and it caches a function
response only with s-maxage). Without a call, the security middleware's ``no-store``
default applies (app.core.middleware).

Rule: never call it on a route with a user dependency (``get_current_user`` or
``get_optional_user``) or on one that sets a cookie: a shared cache would hand one user's
response to another. Such responses use :data:`PRIVATE_NO_STORE`.

:func:`shared_revalidated` is for a small answer every browser must ask again each time
while the CDN shares it briefly (GET /api/forecasts/version). :func:`no_store` keeps a
public response out of every cache, the CDN's included (a public GET whose ``?v=`` names
another forecast version than the one it was answered with).
"""

from __future__ import annotations

from fastapi import Response

PRIVATE_NO_STORE = "private, no-store"
NO_STORE = "no-store"


def cache_headers(
    response: Response,
    *,
    browser_seconds: int,
    cdn_seconds: int | None = None,
    stale_while_revalidate: int = 0,
    immutable: bool = False,
) -> None:
    """Mark ``response`` cacheable by browsers for ``browser_seconds`` and, when
    ``cdn_seconds`` is given, by the CDN for that long (serving a stale copy for up to
    ``stale_while_revalidate`` seconds while it refreshes). ``immutable``: the URL's content
    never changes (e.g. it names the model run)."""
    if browser_seconds < 0 or (cdn_seconds is not None and cdn_seconds < 0):
        raise ValueError("cache lifetimes must be >= 0")
    if stale_while_revalidate < 0:
        raise ValueError("stale_while_revalidate must be >= 0")
    browser = f"public, max-age={browser_seconds}"
    response.headers["Cache-Control"] = f"{browser}, immutable" if immutable else browser
    if cdn_seconds is None:
        return
    cdn = [f"public, s-maxage={cdn_seconds}"]
    if stale_while_revalidate:
        cdn.append(f"stale-while-revalidate={stale_while_revalidate}")
    if immutable:
        cdn.append("immutable")
    response.headers["CDN-Cache-Control"] = ", ".join(cdn)


def shared_revalidated(
    response: Response, *, cdn_seconds: int, stale_while_revalidate: int = 0
) -> None:
    """Browsers revalidate every time (``no-cache``); the CDN shares the response for
    ``cdn_seconds`` (and serves its stale copy for up to ``stale_while_revalidate`` seconds
    while it refreshes)."""
    if cdn_seconds < 0 or stale_while_revalidate < 0:
        raise ValueError("cache lifetimes must be >= 0")
    response.headers["Cache-Control"] = "no-cache"
    cdn = [f"public, s-maxage={cdn_seconds}"]
    if stale_while_revalidate:
        cdn.append(f"stale-while-revalidate={stale_while_revalidate}")
    response.headers["CDN-Cache-Control"] = ", ".join(cdn)


def no_store(response: Response) -> None:
    """Neither browsers nor a shared CDN keep the response (``no-store`` in Cache-Control
    and in CDN-Cache-Control, which Vercel's CDN reads first)."""
    response.headers["Cache-Control"] = NO_STORE
    response.headers["CDN-Cache-Control"] = NO_STORE
