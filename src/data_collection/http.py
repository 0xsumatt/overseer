"""Thin async HTTP client for exchange REST endpoints.

Wraps aiosonic and folds in the three things every scraper needs but should
never re-implement per venue:

  * **proactive rate limiting** — a :class:`RateLimiter` is applied to every
    request, so a call can't accidentally skip it;
  * **retries with exponential backoff + full jitter** on transient failures
    (timeouts, dropped connections, 5xx, 429);
  * **reactive backoff** — a 429 honours ``Retry-After`` *and* pushes that pause
    back into the limiter, so the whole client slows down, not just this call.

Non-retryable responses (4xx other than 429) raise immediately — retrying a
400/401/404 only wastes your rate budget and the venue's patience.

This module is the single place that knows about aiosonic. Swap the HTTP
library and this is the only file that changes.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from aiosonic import HTTPClient, TCPConnector
from aiosonic import exceptions as _aio
from aiosonic.timeout import Timeouts
from data_collection.ratelimit import RateLimiter

# Transient failures worth retrying. Anything outside this tuple (e.g. a bug in
# our own callback, a programming error) propagates instead of being retried.
_RETRYABLE_EXC: tuple[type[BaseException], ...] = (
    _aio.BaseTimeout,
    _aio.ConnectionDisconnected,
    _aio.HttpParsingError,
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
)


class HttpError(Exception):
    """Raised for non-retryable responses or once retries are exhausted."""

    def __init__(self, status: int | None, message: str, body: str = "") -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body


def _header(resp: Any, name: str) -> str | None:
    """Case-insensitive header lookup (aiosonic's HttpHeaders preserves case)."""
    target = name.lower()
    for key, value in resp.headers.items():
        if key.lower() == target:
            return value
    return None


def _retry_after(resp: Any) -> float | None:
    raw = _header(resp, "retry-after")
    if raw is None:
        return None
    try:
        return float(raw)          # delta-seconds form (what exchanges send)
    except ValueError:
        return None                # http-date form — fall back to normal backoff


class HttpClient:
    """Rate-limited, retrying async HTTP client. One per exchange.

    The exchange's :class:`RateLimiter` is passed in and applied to every
    request, so the per-exchange scrapers just call :meth:`get_json` and never
    have to remember to throttle.
    """

    def __init__(
        self,
        *,
        limiter: RateLimiter | None = None,
        default_headers: dict[str, str] | None = None,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        connect_timeout: float = 5.0,
        read_timeout: float = 20.0,
    ) -> None:
        self._limiter = limiter
        self._headers = default_headers or {}
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._timeouts = Timeouts(sock_connect=connect_timeout, sock_read=read_timeout)
        self._client = HTTPClient(connector=TCPConnector(timeouts=self._timeouts))

    def _backoff(self, attempt: int) -> float:
        # exponential with full jitter: uniform in [0, min(cap, base * 2**attempt)]
        ceiling = min(self._backoff_max, self._backoff_base * (2 ** attempt))
        return random.uniform(0.0, ceiling)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        json: Any | None = None,
        data: Any | None = None,
        weight: float = 1.0,
        retry: bool = True,
    ) -> Any:
        """Perform a request, rate-limited and (by default) retried.

        ``weight`` is how many tokens this call costs the limiter — set it above
        1 for endpoints the venue prices more heavily (deep books, batch calls).

        ``json`` sends a JSON body (aiosonic sets Content-Type for you); ``data``
        sends a form/raw body. Body kwargs are only forwarded to methods that
        accept them, so GET stays clean.

        ``retry`` defaults to True, which is correct for the read-style GET/POST
        *queries* this client is built for. Set it False for any call that isn't
        safe to repeat (e.g. if the project ever grows order placement) so the
        first failure raises instead of being retried blindly.
        """
        merged = {**self._headers, **(headers or {})}
        call = getattr(self._client, method.lower())
        max_retries = self._max_retries if retry else 0

        # Only pass a body to methods that take one — GET has no json/data param.
        body_kwargs: dict[str, Any] = {}
        if json is not None:
            body_kwargs["json"] = json
        if data is not None:
            body_kwargs["data"] = data

        attempt = 0
        while True:
            if self._limiter is not None:
                await self._limiter.acquire(weight)

            try:
                resp = await call(
                    url,
                    params=params,
                    headers=merged,
                    timeouts=self._timeouts,
                    **body_kwargs,
                )
            except _RETRYABLE_EXC as exc:
                if attempt >= max_retries:
                    raise HttpError(
                        None, f"network error after {attempt} retries: {exc!r}"
                    ) from exc
                await asyncio.sleep(self._backoff(attempt))
                attempt += 1
                continue

            status = resp.status_code

            if 200 <= status < 300:
                return resp

            if status == 429:
                wait = _retry_after(resp)
                # Push the server's backoff into the limiter so the *whole*
                # client slows down, not just this one call.
                if wait is not None and self._limiter is not None:
                    self._limiter.pause(wait)
                if attempt >= max_retries:
                    raise HttpError(429, "rate limited", await resp.text())
                await asyncio.sleep(wait if wait is not None else self._backoff(attempt))
                attempt += 1
                continue

            if status >= 500:
                if attempt >= max_retries:
                    raise HttpError(status, "server error", await resp.text())
                await asyncio.sleep(self._backoff(attempt))
                attempt += 1
                continue

            # 4xx other than 429: the request itself is wrong — don't burn
            # retries (or rate budget) hammering it.
            raise HttpError(status, "client error", await resp.text())

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        weight: float = 1.0,
    ) -> Any:
        resp = await self.request(
            "get", url, params=params, headers=headers, weight=weight
        )
        return await resp.json()

    async def post_json(
        self,
        url: str,
        *,
        json: Any | None = None,
        data: Any | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        weight: float = 1.0,
        retry: bool = True,
    ) -> Any:
        """POST a body and parse the JSON response.

        Pass ``json=`` for a JSON body (the common case — JSON-RPC nodes and
        POST-style query endpoints) or ``data=`` for a form/raw body. Body
        signing for authenticated endpoints stays in the adapter; this client
        just sends what it's handed.
        """
        resp = await self.request(
            "post",
            url,
            params=params,
            headers=headers,
            json=json,
            data=data,
            weight=weight,
            retry=retry,
        )
        return await resp.json()

    async def aclose(self) -> None:
        """Best-effort connection-pool teardown."""
        connector = getattr(self._client, "connector", None)
        cleanup = getattr(connector, "cleanup", None)
        if cleanup is not None:
            result = cleanup()
            if asyncio.iscoroutine(result):
                await result

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()