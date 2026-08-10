from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from time import monotonic

from flask import Response


@dataclass(frozen=True, slots=True)
class _CachedResponse:
    body: bytes
    status: int
    headers: tuple[tuple[str, str], ...]
    stored_at: float
    expires_at: float


class ResponseCache:
    """Small process-local TTL cache for public GET responses.

    Gunicorn workers intentionally keep separate caches: market-data responses are
    cheap to refill, and avoiding shared cache infrastructure keeps a cache outage
    from becoming an application outage. Reverse-proxy caching can sit in front of
    the same Cache-Control contract later.
    """

    def __init__(
        self,
        ttl_seconds: int = 30,
        max_entries: int = 512,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries: dict[str, _CachedResponse] = {}
        self._lock = Lock()

    def get(self, key: str) -> Response | None:
        now = monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._entries.pop(key, None)
                return None

        response = Response(entry.body, status=entry.status)
        for name, value in entry.headers:
            response.headers[name] = value
        response.headers["Age"] = str(max(0, int(now - entry.stored_at)))
        response.headers["X-Overseer-Cache"] = "HIT"
        return response

    def put(self, key: str, response: Response) -> Response:
        if response.status_code != 200:
            return response

        now = monotonic()
        response.headers["Cache-Control"] = (
            f"public, max-age={self.ttl_seconds}, stale-while-revalidate={self.ttl_seconds}"
        )
        response.headers["X-Overseer-Cache"] = "MISS"
        stored_headers = tuple(
            (name, value)
            for name, value in response.headers.items()
            if name.lower()
            not in {"age", "content-length", "set-cookie", "x-overseer-cache"}
        )
        body = response.get_data()
        if len(body) > self.max_bytes:
            response.headers["X-Overseer-Cache"] = "BYPASS"
            return response
        entry = _CachedResponse(
            body=body,
            status=response.status_code,
            headers=stored_headers,
            stored_at=now,
            expires_at=now + self.ttl_seconds,
        )
        with self._lock:
            expired = [
                cached_key
                for cached_key, cached in self._entries.items()
                if cached.expires_at <= now
            ]
            for cached_key in expired:
                self._entries.pop(cached_key, None)
            self._entries.pop(key, None)
            total_bytes = sum(len(cached.body) for cached in self._entries.values())
            while self._entries and (
                len(self._entries) >= self.max_entries
                or total_bytes + len(entry.body) > self.max_bytes
            ):
                oldest_key = min(
                    self._entries, key=lambda cached_key: self._entries[cached_key].stored_at
                )
                total_bytes -= len(self._entries[oldest_key].body)
                self._entries.pop(oldest_key)
            self._entries[key] = entry
        return response

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
