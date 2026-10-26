"""Cache headers for responses that are the same for every visitor.

:func:`cache_headers` sets ``Cache-Control`` for browsers and, when ``cdn_seconds`` is given,
``CDN-Cache-Control`` for a shared CDN (Vercel's CDN reads it before Cache-Control:
Vercel-CDN-Cache-Control > CDN-Cache-Control > Cache-Control, and it caches a function
response only with s-maxage). Without a call, the security middleware's ``no-store``
default applies (app.core.middleware).

Rule: never call it on a route with a user dependency (``get_current_user`` or
``get_optional_user``) or on one that sets a cookie: a shared cache would hand one user's
response to another. Such responses use :data:`PRIVATE_NO_STORE`.
"""

from __future__ import annotations

from fastapi import Response

PRIVATE_NO_STORE = "private, no-store"


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
