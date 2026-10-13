"""The API container's uvicorn options must not override TRUSTED_PROXY_COUNT.

Rate limits key on :func:`app.core.ratelimit.client_ip`: the X-Forwarded-For entry that the
TRUSTED_PROXY_COUNT proxies appended, or the socket peer when TRUSTED_PROXY_COUNT=0. Seen
on 2026-10-13 with the image's former options (``--proxy-headers
--forwarded-allow-ips="*"``) and TRUSTED_PROXY_COUNT=0: uvicorn replaced the socket peer by
the left-most, client-written X-Forwarded-For entry, so 70 requests to /api/health/data
(limit 60 a minute) with a different made-up header each all passed (70 x 200), while 70
without the header gave 60 x 200 and 10 x 429.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from pathlib import Path
from typing import Any

from uvicorn.config import Config
from uvicorn.main import main as uvicorn_cli

ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker-entrypoint.sh"


def api_command() -> list[str]:
    """The arguments of the ``api`` role's uvicorn command, defaults substituted."""
    lines = [
        line.strip()
        for line in ENTRYPOINT.read_text().splitlines()
        if line.strip().startswith("exec uvicorn app.main:app")
    ]
    assert len(lines) == 1, lines
    # ${NAME:-default} -> default (the value the container uses when NAME is unset).
    line = re.sub(r"\$\{[A-Z_]+:-([^}]*)\}", r"\1", lines[0])
    args = shlex.split(line)
    return args[2:]  # drop "exec uvicorn"


def seen_client(args: list[str], peer: str, forwarded_for: str) -> str:
    """The client address the app sees under these uvicorn options."""
    params = uvicorn_cli.make_context("uvicorn", list(args)).params
    seen: dict[str, Any] = {}

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen["client"] = scope["client"]

    config = Config(
        app,
        proxy_headers=params["proxy_headers"],
        forwarded_allow_ips=params["forwarded_allow_ips"],
        lifespan="off",
    )
    config.load()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/health/data",
        "headers": [(b"x-forwarded-for", forwarded_for.encode())],
        "client": (peer, 40000),
        "scheme": "http",
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    async def send(_: dict[str, Any]) -> None:
        return None

    asyncio.run(config.loaded_app(scope, receive, send))
    return str(seen["client"][0])


def test_api_command_parses() -> None:
    args = api_command()
    assert args[0] == "app.main:app"
    params = uvicorn_cli.make_context("uvicorn", list(args)).params
    assert params["port"] == 8000 and params["workers"] == 2


def test_uvicorn_keeps_the_socket_peer() -> None:
    """A client-written X-Forwarded-For never replaces the socket address; reading the
    header is left to client_ip and TRUSTED_PROXY_COUNT."""
    assert seen_client(api_command(), "172.18.0.5", "198.51.100.7") == "172.18.0.5"
