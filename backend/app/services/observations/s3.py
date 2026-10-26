"""Anonymous S3 access over HTTPS: ListObjectsV2 listing and object URLs.

The open-data buckets (AODN, Met Office) are public; no credentials or AWS SDK are needed.
Listing uses the ListObjectsV2 REST API (``?list-type=2&prefix=...``) and follows
``NextContinuationToken`` until ``IsTruncated`` is false. The response is parsed with
regular expressions rather than an XML parser: the document shape is fixed and this avoids
entity-expansion concerns with untrusted XML.

Keys are returned exactly as stored. Some AODN keys contain a literal ``%20`` (Hive
partition escaping), so an object URL must percent-encode the key again ("%2520");
:func:`object_url` does that (research notes, "Forecast validation methodology", AODN).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.services.http import PermanentProviderError, request_with_retries

_CONTENTS = re.compile(r"<Contents>(.*?)</Contents>", re.S)
_PREFIX = re.compile(r"<CommonPrefixes>\s*<Prefix>(.*?)</Prefix>\s*</CommonPrefixes>", re.S)
_TAG = {
    name: re.compile(rf"<{name}>(.*?)</{name}>", re.S)
    for name in ("Key", "Size", "NextContinuationToken", "IsTruncated")
}

MAX_PAGES = 200  # 200 000 keys: a safety stop, far above any listing used here


@dataclass(frozen=True)
class S3Object:
    key: str
    size: int


@dataclass
class S3Listing:
    objects: list[S3Object]
    prefixes: list[str]
    requests: int


def _tag(name: str, text: str) -> str | None:
    m = _TAG[name].search(text)
    return html.unescape(m.group(1)) if m else None


def object_url(base_url: str, key: str) -> str:
    """HTTPS URL of an object; the key is percent-encoded (a literal '%' becomes '%25')."""
    return f"{base_url.rstrip('/')}/{quote(key, safe='/=')}"


def list_objects(
    client: httpx.Client,
    base_url: str,
    prefix: str,
    *,
    provider: str,
    delimiter: str | None = None,
    start_after: str | None = None,
    attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> S3Listing:
    """List keys (and common prefixes when ``delimiter`` is set) under ``prefix``."""
    objects: list[S3Object] = []
    prefixes: list[str] = []
    token: str | None = None
    requests = 0
    for _ in range(MAX_PAGES):
        params: dict[str, str] = {"list-type": "2", "prefix": prefix}
        if delimiter:
            params["delimiter"] = delimiter
        if token:
            params["continuation-token"] = token
        elif start_after:
            params["start-after"] = start_after
        requests += 1
        response = request_with_retries(
            client,
            "GET",
            base_url.rstrip("/") + "/",
            provider=provider,
            attempts=attempts,
            backoff_seconds=backoff_seconds,
            params=params,
        )
        if response.status_code != 200:
            raise PermanentProviderError(
                f"{provider}: listing {prefix!r} failed with HTTP {response.status_code}"
            )
        body = response.text
        for block in _CONTENTS.findall(body):
            key = _tag("Key", block)
            if key is None:
                continue
            objects.append(S3Object(key, int(_tag("Size", block) or 0)))
        prefixes.extend(html.unescape(p) for p in _PREFIX.findall(body))
        truncated = (_tag("IsTruncated", body) or "false").lower() == "true"
        token = _tag("NextContinuationToken", body)
        if not truncated or not token:
            return S3Listing(objects, prefixes, requests)
    raise PermanentProviderError(f"{provider}: listing {prefix!r} exceeded {MAX_PAGES} pages")
