from __future__ import annotations

import asyncio
import atexit
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import threading
import time

from core.positions import PositionReadError, number
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter
from data_collection.positions.bulk import BulkPositions
from data_collection.positions.hyperliquid import HyperliquidPositions
from data_collection.positions.lighter import LighterPositions
from data_collection.positions.risex import RisexPositions


class ReaderBusy(PositionReadError):
    pass


def json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


class PositionReader:
    """Lazy process-local public clients; account reads exist only on the call stack."""

    deadline = 25

    def __init__(self):
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(2)
        self._loop = None
        self._thread = None
        self._http = None
        self._adapters = {}
        self._fx = None
        self._fx_at = 0.0
        self._closed = False
        atexit.register(self.close)

    def _start(self):
        with self._lock:
            if self._closed:
                raise PositionReadError("Position reader is unavailable.")
            if self._loop is None:
                loop = asyncio.new_event_loop()
                self._loop = loop
                self._thread = threading.Thread(target=loop.run_forever, name="position-reader", daemon=True)
                self._thread.start()

    async def _clients(self):
        if self._http is None:
            self._http = HttpClient(limiter=RateLimiter.per_minute(60, burst=2), max_retries=0,
                                    connect_timeout=3, read_timeout=20)
            self._adapters = {
                "hyperliquid": HyperliquidPositions(self._http),
                "lighter": LighterPositions(self._http),
                "bulk": BulkPositions(self._http),
                "risex": RisexPositions(self._http),
            }

    def _call(self, operation, *args):
        if not self._slots.acquire(blocking=False):
            raise ReaderBusy("Position reader is busy. Try again shortly.")
        submitted = False
        try:
            self._start()
            future = asyncio.run_coroutine_threadsafe(self._bounded(operation, args), self._loop)
            submitted = True
            try:
                return future.result(timeout=self.deadline + 2)
            except FutureTimeout:
                future.cancel()
                raise PositionReadError("Position read timed out. Try again shortly.") from None
            except PositionReadError:
                raise
            except Exception:
                raise PositionReadError("Position data is unavailable. Try again shortly.") from None
        finally:
            # Once submitted, only the coroutine releases admission, including
            # cancellation. A timed-out sync caller must not admit extra work.
            if not submitted:
                self._slots.release()

    async def _bounded(self, operation, args):
        try:
            async with asyncio.timeout(self.deadline):
                await self._clients()
                return await operation(*args)
        except TimeoutError:
            raise PositionReadError("Position read timed out. Try again shortly.") from None
        except PositionReadError:
            raise
        except Exception:
            # Do not propagate provider bodies, URLs, credentials, or tracebacks.
            raise PositionReadError("Position data is unavailable. Try again shortly.") from None
        finally:
            self._slots.release()

    def read(self, venue, address, kind, since=None, until=None, cursor=None, token=None):
        return self._call(self._read, venue, address, kind, since, until, cursor, token)

    async def _read(self, venue, address, kind, since, until, cursor, token):
        adapter = self._adapters[venue]
        if kind == "positions":
            return json_value(asdict(await adapter.fetch_positions(address)))
        if cursor is not None:
            if cursor.get("address") != address:
                raise ValueError("Invalid cursor owner")
            if venue == "hyperliquid":
                start, end = int(since.timestamp() * 1000), int(until.timestamp() * 1000)
                if cursor.get("since") != start or cursor.get("end") != end or not start <= cursor.get("start", -1) <= end:
                    raise ValueError("Invalid cursor window")
            elif venue == "lighter":
                if cursor.get("since") != int(since.timestamp()) or cursor.get("until") != int(until.timestamp()):
                    raise ValueError("Invalid cursor window")
            elif venue == "bulk":
                if cursor.get("since") != since.isoformat() or cursor.get("until") != until.isoformat():
                    raise ValueError("Invalid cursor window")
            else:
                epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
                def ns(value):
                    delta = value - epoch
                    return str((delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000)
                if cursor.get("start") != ns(since) or cursor.get("end") != ns(until):
                    raise ValueError("Invalid cursor window")
        page = await adapter.fetch_funding(address, since, until, cursor=cursor, token=token)
        if not isinstance(page.complete, bool) or (page.complete and page.cursor is not None):
            raise ValueError("Invalid completion")
        if not page.complete and (not isinstance(page.cursor, dict) or not page.cursor or page.cursor == cursor):
            raise ValueError("Nonprogressing funding page")
        if page.coverage_since is not None and not since <= page.coverage_since <= until:
            raise ValueError("Invalid coverage")
        seen = {}
        for event in page.events:
            if not since <= event.ts <= until or not event.event_id:
                raise ValueError("Invalid event boundary")
            if event.event_id in seen and seen[event.event_id] != event:
                raise ValueError("Conflicting event")
            seen[event.event_id] = event
        return json_value({"events": [asdict(event) for event in seen.values()],
                           "cursor": page.cursor, "complete": page.complete,
                           "coverage_since": page.coverage_since, "coverage_note": page.coverage_note})

    def discover_accounts(self, venue, wallet, cursor=None):
        return self._call(self._resolve, venue, wallet, cursor)

    async def _resolve(self, venue, wallet, cursor):
        result = await self._adapters[venue].resolve_accounts(wallet, cursor=cursor)
        return {"venue": venue, **result}

    def fx(self):
        return self._call(self._exchange_rates)

    async def _exchange_rates(self):
        if self._fx is not None and time.monotonic() - self._fx_at < 300:
            return dict(self._fx, rates=dict(self._fx["rates"]))
        payload = await self._http.get_json("https://api.coinbase.com/v2/exchange-rates", params={"currency": "USD"})
        if payload["data"]["currency"] != "USD":
            raise ValueError("Invalid FX base")
        rates = {}
        for currency, value in payload["data"]["rates"].items():
            rate = number(value)
            if rate <= 0 or abs(rate.adjusted()) > 60 or len(rate.as_tuple().digits) > 80:
                raise ValueError("Invalid FX rate")
            rates[currency] = str(Decimal(1) / rate)
        self._fx = {"ts": datetime.now(timezone.utc).isoformat(), "rates": rates,
                    "source": "Coinbase USD exchange rates"}
        self._fx_at = time.monotonic()
        return dict(self._fx, rates=dict(rates))

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
        if loop is not None and loop.is_running():
            async def shutdown():
                tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if self._http is not None:
                    await self._http.aclose()
            future = asyncio.run_coroutine_threadsafe(shutdown(), loop)
            try:
                future.result(timeout=2)
            except Exception:
                future.cancel()
            loop.call_soon_threadsafe(loop.stop)
