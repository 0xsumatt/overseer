"""Lazy, bounded synchronous access to transient public execution snapshots."""
from __future__ import annotations

import asyncio
import atexit
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import threading
import time

from aiosonic.exceptions import BaseTimeout

from data_collection.execution import DecimalHttpClient, DepthUnavailable, ExecutionDepth, number
from data_collection.http import HttpError
from data_collection.ratelimit import RateLimiter

FX_URL = "https://api.coinbase.com/v2/exchange-rates"


class ReaderFailure(RuntimeError):
    pass


class ReaderBusy(ReaderFailure):
    pass


class _Admission:
    """A queued cancellation may precede the coroutine's first instruction."""

    def __init__(self, slots):
        self.slots = slots
        self.lock = threading.Lock()
        self.started = False
        self.released = False

    def start(self):
        with self.lock:
            if self.released:
                return False
            self.started = True
            return True

    def release(self):
        with self.lock:
            if not self.released:
                self.released = True
                self.slots.release()

    def submitted_done(self, _future):
        # The concurrent future can finish cancellation before async cleanup.
        # Only a never-started coroutine may surrender its slot here; a started
        # coroutine owns release until its children have finished cancellation.
        with self.lock:
            if not self.started and not self.released:
                self.released = True
                self.slots.release()


class ExecutionReader:
    """No network, clients or background thread until capture() is called."""

    deadline = 20
    individual_timeout = 8
    parallelism = 4
    fx_ttl = 30

    def __init__(self):
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(2)
        self._loop = None
        self._thread = None
        self._clients = {}
        self._active = set()
        self._provider = None
        self._parallel = None
        self._fx_lock = None
        self._fx = None
        self._fx_at = 0.0
        self._closed = False
        atexit.register(self.close)

    def _start_locked(self):
        if self._closed:
            raise ReaderFailure("Execution reader is unavailable.")
        if self._loop is None:
            loop = asyncio.new_event_loop()
            ready = threading.Event()
            loop.call_soon(ready.set)
            self._loop = loop
            def run():
                try:
                    loop.run_forever()
                finally:
                    loop.close()
            self._thread = threading.Thread(target=run, name="execution-reader", daemon=True)
            self._thread.start()
            # close() must never observe a published loop that has not started.
            ready.wait()

    async def _ensure_clients(self):
        if self._provider is not None:
            return
        # Separate venue buckets; Hyperliquid native and deployers share one.
        limits = {
            "binance_perp": (RateLimiter.per_minute(1200, burst=80)),
            "bybit_perp": RateLimiter.per_second(5, burst=10),
            "hyperliquid": RateLimiter.per_minute(600, burst=60),
            "lighter": RateLimiter.per_minute(300, burst=20),
            "extended": RateLimiter.per_second(5, burst=10),
            "rise": RateLimiter.per_minute(300, burst=20),
            "bullet": RateLimiter.per_second(5, burst=10),
            "bulk": RateLimiter.per_minute(300, burst=20),
            "fx": RateLimiter.per_minute(60, burst=2),
        }
        for venue, limiter in limits.items():
            self._clients[venue] = DecimalHttpClient(limiter=limiter, max_retries=0,
                                                    connect_timeout=4, read_timeout=8,
                                                    default_headers={"User-Agent": "overseer/0.1"})
        self._provider = ExecutionDepth(self._clients)
        self._parallel = asyncio.Semaphore(self.parallelism)
        self._fx_lock = asyncio.Lock()

    def capture(self, listings: dict[str, str], *, margin: bool = False) -> dict:
        """Public books for each listed venue, with each venue's margin schedule
        when `margin` is set. A margin read never costs a venue its book."""
        if not isinstance(listings, dict) or len(listings) > 10 or any(
                not isinstance(venue, str) or not isinstance(symbol, str) or not symbol
                for venue, symbol in listings.items()):
            raise ReaderFailure("Invalid public market listings.")
        if not self._slots.acquire(blocking=False):
            raise ReaderBusy("Execution reader is busy. Try again shortly.")
        admission = _Admission(self._slots)
        submitted = False
        try:
            with self._lock:
                self._start_locked()
                coroutine = self._bounded(dict(listings), admission, margin)
                try:
                    future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
                except Exception:
                    coroutine.close()
                    raise
                submitted = True
                future.add_done_callback(admission.submitted_done)
            try:
                return future.result(timeout=self.deadline + 2)
            except FutureTimeout:
                future.cancel()
                raise ReaderFailure("Execution capture timed out. Try again shortly.") from None
            except ReaderFailure:
                raise
            except Exception:
                raise ReaderFailure("Public execution data is unavailable. Try again shortly.") from None
        finally:
            if not submitted:
                admission.release()

    async def _bounded(self, listings, admission, margin=False):
        if not admission.start():
            raise asyncio.CancelledError
        task = asyncio.current_task()
        self._active.add(task)
        try:
            await self._ensure_clients()
            return await self._capture(listings, margin)
        except ReaderFailure:
            raise
        except Exception:
            raise ReaderFailure("Public execution data is unavailable. Try again shortly.") from None
        finally:
            self._active.discard(task)
            admission.release()

    async def _venue(self, venue, symbol):
        async with self._parallel:
            try:
                async with asyncio.timeout(self.individual_timeout):
                    return await self._provider.fetch(venue, symbol), None
            except TimeoutError:
                return None, "Public depth request timed out."
            except DepthUnavailable as exc:
                return None, str(exc)
            except HttpError as exc:
                return None, self._http_error(exc, "Public depth")
            except Exception:
                return None, "Public depth is unavailable from this provider."

    async def _margin(self, venue, symbol):
        async with self._parallel:
            try:
                async with asyncio.timeout(self.individual_timeout):
                    return await self._provider.margin(venue, symbol), None
            except TimeoutError:
                return None, "Public margin request timed out."
            except DepthUnavailable as exc:
                return None, str(exc)
            except HttpError as exc:
                return None, self._http_error(exc, "Public margin")
            except Exception:
                return None, "Public margin data is unavailable from this provider."

    @staticmethod
    def _http_error(exc, subject):
        # HttpError text/body may contain upstream URLs, payloads or credentials.
        # Its typed status and transport cause are sufficient for public errors.
        if isinstance(exc.__cause__, (TimeoutError, BaseTimeout)):
            return f"{subject} request timed out."
        if exc.status == 429:
            return f"{subject} provider rate limited the request (HTTP 429)."
        if exc.status is not None:
            return f"{subject} provider returned HTTP {exc.status}."
        return f"{subject} request failed due to a network error."

    @staticmethod
    def _fx_error(message):
        return {"rates": {"USD": "1"}, "ts": None,
                "source": "Coinbase USD reference exchange rates", "source_url": FX_URL,
                "error": message}

    async def _exchange_rates(self):
        try:
            async with asyncio.timeout(self.individual_timeout):
                async with self._fx_lock:
                    if self._fx is not None and time.monotonic() - self._fx_at < self.fx_ttl:
                        return dict(self._fx, rates=dict(self._fx["rates"]))
                    payload = await self._clients["fx"].get_json(FX_URL, params={"currency": "USD"})
                    data = payload["data"]
                    if data["currency"] != "USD":
                        raise ValueError("Wrong FX base")
                    rates = {"USD": "1"}
                    with localcontext() as context:
                        context.prec = 40
                        for currency in ("USDC", "USDT"):
                            if currency in data["rates"]:
                                rates[currency] = str(Decimal(1) / number(data["rates"][currency]))
                    missing = [currency for currency in ("USDC", "USDT") if currency not in rates]
                    result = {"rates": rates, "ts": datetime.now(timezone.utc).isoformat(),
                              "source": "Coinbase USD reference exchange rates", "source_url": FX_URL,
                              "error": "Reference FX is missing for " + ", ".join(missing) + "." if missing else None}
                    self._fx = result
                    self._fx_at = time.monotonic()
                    return dict(result, rates=dict(rates))
        except TimeoutError:
            return self._fx_error("Current USD reference FX request timed out.")
        except HttpError as exc:
            return self._fx_error(self._http_error(exc, "Current USD reference FX"))
        except Exception:
            # Never return stale cached rates as a success after a failed refresh.
            return self._fx_error("Current USD reference FX is unavailable.")

    @staticmethod
    async def _cancel_and_wait(tasks):
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        cleanup = asyncio.gather(*tasks, return_exceptions=True)
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # A second caller/shutdown cancellation must not abort the
                # children's first cancellation cleanup and release admission.
                interrupted = True
        if interrupted:
            raise asyncio.CancelledError

    async def _capture(self, listings, margin=False):
        tasks = {venue: asyncio.create_task(self._venue(venue, symbol)) for venue, symbol in listings.items()}
        margin_tasks = ({venue: asyncio.create_task(self._margin(venue, symbol)) for venue, symbol in listings.items()}
                        if margin else {})
        fx_task = asyncio.create_task(self._exchange_rates())
        all_tasks = [*tasks.values(), *margin_tasks.values(), fx_task]
        try:
            done, pending = await asyncio.wait(all_tasks, timeout=self.deadline)
            # Admission and worker slots remain owned until cancellation ends.
            await self._cancel_and_wait(pending)
            books, errors = [], {}
            for venue, task in tasks.items():
                if task not in done or task.cancelled():
                    errors[venue] = "Public depth exceeded the overall capture deadline."
                    continue
                try:
                    book, error = task.result()
                except Exception:
                    errors[venue] = "Public depth is unavailable from this provider."
                    continue
                if book is not None:
                    books.append(book)
                if error is not None:
                    errors[venue] = error
            fx = fx_task.result() if fx_task in done and not fx_task.cancelled() else self._fx_error(
                "Current USD reference FX exceeded the capture deadline.")
            result = {"books": books, "errors": errors, "fx": fx}
            if margin:
                result["margins"], result["margin_errors"] = {}, {}
                for venue, task in margin_tasks.items():
                    schedule, error = (task.result() if task in done and not task.cancelled()
                                       else (None, "Public margin data exceeded the overall capture deadline."))
                    if schedule is not None:
                        result["margins"][venue] = schedule
                    else:
                        result["margin_errors"][venue] = error
            return result
        finally:
            await self._cancel_and_wait(all_tasks)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._thread
        if loop is None:
            return
        async def shutdown():
            try:
                # Cancel roots only: cancelling both a root and its children
                # can interrupt the children's asynchronous cleanup twice.
                await self._cancel_and_wait(tuple(self._active))
                await asyncio.gather(*(client.aclose() for client in self._clients.values()),
                                     return_exceptions=True)
            finally:
                # Stop only after all client-close attempts, even if the sync
                # close caller has stopped waiting. Never cancel cleanup early.
                loop.call_soon(loop.stop)
        if loop.is_running():
            future = asyncio.run_coroutine_threadsafe(shutdown(), loop)
            try:
                future.result(timeout=self.deadline + 2)
            except Exception:
                pass
            if thread is not threading.current_thread():
                thread.join(timeout=2)
