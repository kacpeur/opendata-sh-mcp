"""Thin async client for the CKAN Action API behind opendata.schleswig-holstein.de."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__

DEFAULT_BASE_URL = "https://opendata.schleswig-holstein.de"
# The portal sits behind the Anubis bot filter, which challenges browser-like
# user agents ("Mozilla/...") and denies AI-assistant ones ("Claude-User", "ChatGPT-User").
# A plain, descriptive agent passes as an API client.
DEFAULT_USER_AGENT = f"opendata-sh-mcp/{__version__} (MCP server; python-httpx)"
# Metadata answers are a few KB each; a long-running HTTP server must not keep them forever.
MAX_CACHE_ENTRIES = 256
# Overload and gateway errors are worth another try; other statuses carry a real answer.
RETRY_STATUSES = (429, 502, 503, 504)


def _describe(exc: Exception) -> str:
    """httpx timeouts have an empty message, so always name the kind of failure."""
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


class PortalError(ToolError):
    """Raised when the portal returns an error or something that is not CKAN JSON.

    Subclassing ToolError lets the message reach the model instead of a generic crash text.
    """


class _RetryableStatus(Exception):
    """A download answered with an overload status and attempts remain."""


@dataclass
class _CacheEntry:
    expires: float
    value: Any


@dataclass
class CkanClient:
    base_url: str = field(default_factory=lambda: os.environ.get("OPENDATA_SH_BASE_URL", DEFAULT_BASE_URL))
    user_agent: str = field(default_factory=lambda: os.environ.get("OPENDATA_SH_USER_AGENT", DEFAULT_USER_AGENT))
    timeout: float = field(default_factory=lambda: float(os.environ.get("OPENDATA_SH_TIMEOUT", "30")))
    cache_ttl: float = field(default_factory=lambda: float(os.environ.get("OPENDATA_SH_CACHE_TTL", "300")))
    transport: httpx.AsyncBaseTransport | None = None
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)
    _cache: dict[str, _CacheEntry] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                follow_redirects=True,
                transport=self.transport,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def dataset_url(self, name: str) -> str:
        return f"{self.base_url}/dataset/{name}"

    async def action(self, name: str, cache: bool = True, **params: Any) -> Any:
        """Call a CKAN action with GET and return its ``result`` payload."""
        clean = {k: v for k, v in params.items() if v is not None}
        key = f"{name}?{sorted(clean.items())!r}"
        now = time.monotonic()
        if cache and (hit := self._cache.get(key)) and hit.expires > now:
            return hit.value

        url = f"{self.base_url}/api/3/action/{name}"
        resp = await self._get_with_retry(url, clean)
        try:
            body = resp.json()
        except ValueError as exc:
            snippet = resp.text[:200].replace("\n", " ")
            hint = ""
            if "anubis" in resp.text.lower():
                hint = " (looks like a bot-protection page; try another OPENDATA_SH_USER_AGENT)"
            elif resp.status_code == 403:
                hint = (
                    " (blocked by the portal's firewall, which rejects a double quote followed by"
                    " SQL-like words such as create, update, select or drop; rephrase the query)"
                )
            raise PortalError(f"{name}: portal did not return JSON (HTTP {resp.status_code}){hint}: {snippet}") from exc

        if not isinstance(body, dict) or not body.get("success"):
            if isinstance(body, str):  # e.g. "Bad request - Action name not known: ..."
                raise PortalError(f"{name}: {body}")
            if not isinstance(body, dict):
                raise PortalError(f"{name}: unexpected response shape from portal")
            err = body.get("error") or {}
            msg = err.get("message") or err.get("__type") or str(err)
            raise PortalError(f"{name}: {msg}")
        if cache:
            self._store(key, body["result"], now)
        return body["result"]

    def _store(self, key: str, value: Any, now: float) -> None:
        self._cache.pop(key, None)
        if len(self._cache) >= MAX_CACHE_ENTRIES:
            for stale in [k for k, e in self._cache.items() if e.expires <= now]:
                del self._cache[stale]
            while len(self._cache) >= MAX_CACHE_ENTRIES:
                del self._cache[next(iter(self._cache))]  # oldest insertion first
        self._cache[key] = _CacheEntry(now + self.cache_ttl, value)

    # The portal (or the network in front of it) resets roughly one connection in four,
    # so transport errors get a few quick retries.
    async def _get_with_retry(self, url: str, params: dict[str, Any], attempts: int = 4) -> httpx.Response:
        delay = 0.5
        for attempt in range(attempts):
            try:
                resp = await self.http.get(url, params=params)
            except httpx.TransportError as exc:
                if attempt == attempts - 1:
                    raise PortalError(f"network error talking to {self.base_url}: {_describe(exc)}") from exc
            else:
                # CKAN answers 404/409 with a JSON error body worth surfacing, so only retry
                # on overload or server errors.
                if resp.status_code not in RETRY_STATUSES or attempt == attempts - 1:
                    return resp
            await asyncio.sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")

    async def download(self, url: str, max_bytes: int, attempts: int = 4) -> tuple[bytes, str, bool]:
        """Fetch up to ``max_bytes`` of a resource. Returns (data, content_type, truncated)."""
        delay = 0.5
        for attempt in range(attempts):
            try:
                return await self._download_once(url, max_bytes, retry_status=attempt < attempts - 1)
            except _RetryableStatus:
                pass
            except httpx.TransportError as exc:
                if attempt == attempts - 1:
                    raise PortalError(f"network error downloading {url}: {_describe(exc)}") from exc
            await asyncio.sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")

    async def _download_once(self, url: str, max_bytes: int, retry_status: bool = False) -> tuple[bytes, str, bool]:
        chunks: list[bytes] = []
        size = 0
        truncated = False
        async with self.http.stream("GET", url, headers={"Accept": "*/*"}) as resp:
            if retry_status and resp.status_code in RETRY_STATUSES:
                raise _RetryableStatus
            if resp.status_code >= 400:
                raise PortalError(f"download failed with HTTP {resp.status_code}: {url}")
            ctype = resp.headers.get("content-type", "")
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    truncated = True
                    break
        return b"".join(chunks)[:max_bytes], ctype, truncated

    async def resolve(self, url: str, attempts: int = 4) -> str:
        """Follow redirects from ``url`` and return the final URL without reading the body."""
        delay = 0.5
        for attempt in range(attempts):
            try:
                async with self.http.stream("GET", url, headers={"Accept": "text/html"}) as resp:
                    if resp.status_code >= 400:
                        raise PortalError(f"HTTP {resp.status_code}: {url}")
                    return str(resp.url)
            except httpx.TransportError as exc:
                if attempt == attempts - 1:
                    raise PortalError(f"network error opening {url}: {_describe(exc)}") from exc
            await asyncio.sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")
