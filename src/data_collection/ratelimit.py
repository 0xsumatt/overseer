from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """A weighted, async token-bucket limiter.

    Args:
        rate: sustained refill in tokens per second.
        capacity: maximum burst (bucket size). Defaults to ``rate`` — i.e. about
            one second of burst, a conservative default. Raise it for venues that
            tolerate larger bursts.
    """

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self._rate = float(rate)
        self._capacity = float(capacity) if capacity is not None else float(rate)
        if self._capacity <= 0:
            raise ValueError("capacity must be > 0")
        self._tokens = self._capacity            # start full
        self._updated = time.monotonic()
        self._blocked_until = 0.0                # server-imposed backoff (monotonic)
        self._lock = asyncio.Lock()

    # -- convenience constructors -------------------------------------------------

    @classmethod
    def per_second(cls, n: float, burst: float | None = None) -> "RateLimiter":
        return cls(rate=n, capacity=burst)

    @classmethod
    def per_minute(cls, n: float, burst: float | None = None) -> "RateLimiter":
        return cls(rate=n / 60.0, capacity=burst)

    # -- server-signal feedback ---------------------------------------------------

    def pause(self, seconds: float) -> None:
        """Block all acquisitions for ``seconds`` (called on 429 / Retry-After)."""
        if seconds <= 0:
            return
        self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)

    # -- the core -----------------------------------------------------------------

    async def acquire(self, weight: float = 1.0) -> None:
        """Block until ``weight`` tokens are available, then consume them."""
        if weight <= 0:
            raise ValueError("weight must be > 0")
        if weight > self._capacity:
            raise ValueError(
                f"weight {weight} exceeds bucket capacity {self._capacity}; "
                "a single request could never be admitted"
            )

        # Holding the lock across the wait serialises acquirers into a clean FIFO
        # schedule and prevents a thundering-herd resync. The actual HTTP work
        # happens after acquire() returns and the lock is released, so requests
        # still run concurrently up to whatever the bucket admits.
        async with self._lock:
            while True:
                now = time.monotonic()

                # honour a server-imposed pause first
                if now < self._blocked_until:
                    await asyncio.sleep(self._blocked_until - now)
                    continue

                # refill, capped at capacity
                self._tokens = min(
                    self._capacity,
                    self._tokens + (now - self._updated) * self._rate,
                )
                self._updated = now

                if self._tokens >= weight:
                    self._tokens -= weight
                    return

                # not enough yet — wait for the deficit to refill, then re-check
                deficit = weight - self._tokens
                await asyncio.sleep(deficit / self._rate)

    # -- ergonomic ``async with limiter:`` for weight-1 calls ---------------------

    async def __aenter__(self) -> "RateLimiter":
        await self.acquire(1.0)
        return self

    async def __aexit__(self, *exc) -> None:
        return None