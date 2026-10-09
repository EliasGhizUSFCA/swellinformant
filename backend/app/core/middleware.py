"""CSRF protection and security headers (pure ASGI middleware)."""

from __future__ import annotations

import json
from http.cookies import SimpleCookie

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth.security import constant_time_equals
from app.core.config import get_settings

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
# Endpoints authenticated by their own secret instead of the session cookie.
CSRF_EXEMPT_PREFIXES = (
    "/api/webhooks/",
    "/api/notifications/unsubscribe",
    "/api/health",
)


class CSRFMiddleware:
    """Double-submit cookie check for every state-changing /api request.

    The ``sta_csrf`` cookie is readable by our own frontend (same origin via the Next.js
    proxy) and must be echoed in the ``X-CSRF-Token`` header. A cross-site attacker can
    make the browser send cookies but cannot read them to forge the header. SameSite=Lax
    cookies are a second layer.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope["method"].upper()
        path: str = scope["path"]
        if (
            method in SAFE_METHODS
            or not path.startswith("/api/")
            or path.startswith(CSRF_EXEMPT_PREFIXES)
        ):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        cookie_name = get_settings().csrf_cookie_name
        jar = SimpleCookie()
        try:
            jar.load(headers.get("cookie", ""))
        except Exception:  # malformed cookie header
            jar = SimpleCookie()
        cookie_val = jar[cookie_name].value if cookie_name in jar else ""
        header_val = headers.get("x-csrf-token", "")
        if not cookie_val or not header_val or not constant_time_equals(cookie_val, header_val):
            body = json.dumps(
                {
                    "error": {
                        "code": "csrf_failed",
                        "message": "Missing or invalid CSRF token. Refresh the page and try again.",
                    }
                }
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        secure = get_settings().cookie_secure

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    (b"permissions-policy", b"geolocation=(), camera=(), microphone=()"),
                ]
                if not path.startswith(DOC_PATHS):
                    extra.append(
                        (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'")
                    )
                if path.startswith("/api/"):
                    extra.append((b"cache-control", b"no-store"))
                if secure:
                    extra.append(
                        (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
                    )
                message["headers"] = headers + extra
            await send(message)

        await self.app(scope, receive, wrapped)
