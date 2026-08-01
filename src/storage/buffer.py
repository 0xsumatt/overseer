"""Buffered writes for streaming feeds.

Bridges the synchronous world of a picows frame callback to the async world of
asyncpg. `add()` is sync and must stay cheap — it is called from the read loop,
where any blocking stalls frame delivery. `run()` drains on a timer from a
normal async task.

It also fixes an impedance mismatch: Storage._bulk_upsert is one database
round-trip per call, which is right for a poll returning 500 bars and badly
wrong for a per-message write. Batching by size OR age keeps a busy feed
efficient without letting a quiet one sit unflushed indefinitely.

Records are de-duplicated on their natural `key` before writing, because a
resubscribe after a reconnect commonly replays recent messages. The database
would reject the duplicates anyway (the trades primary key is idempotent by
design) — deduping here just avoids paying for the round-trip.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

log = logging.getLogger("overseer.buffer")


# A trade at or above this quote notional counts as "large". Quote-denominated
# so it means the same thing across assets and venues (all of ours quote in a
# USD stablecoin), unlike a base-size threshold which would be meaningless
# across a $65k asset and a $0.40 one.
#
# This is applied AT INGEST and cannot be changed retroactively — the trades it
# classifies are not kept. Changing it makes history incomparable, so treat it
# as a schema decision, not a tunable. max_trade_notional is stored alongside
# precisely to hedge that: it is threshold-free, so a badly chosen threshold
# does not leave the data useless.
LARGE_TRADE_NOTIONAL = Decimal(10_000)


class _Bucket:
    """Mutable accumulator for one market-minute. Plain class with slots
    rather than a dataclass: one of these is touched on every single trade."""

    __slots__ = ("trades", "volume", "buy", "sell", "notional",
                 "large", "large_buy", "large_sell", "max_notional",
                 "first_ts", "open", "high", "low", "last_ts", "close")

    def __init__(self, trade) -> None:
        self.trades = self.large = 0
        self.volume = self.buy = self.sell = self.notional = Decimal(0)
        self.large_buy = self.large_sell = self.max_notional = Decimal(0)
        self.first_ts = self.last_ts = trade.ts
        self.open = self.high = self.low = self.close = trade.price

    def add(self, trade) -> None:
        self.trades += 1
        self.volume += trade.amount
        notional = trade.price * trade.amount
        self.notional += notional
        is_buy = trade.side == "buy"
        if is_buy:
            self.buy += trade.amount
        else:
            self.sell += trade.amount
        if notional > self.max_notional:
            self.max_notional = notional
        if notional >= LARGE_TRADE_NOTIONAL:
            self.large += 1
            if is_buy:
                self.large_buy += trade.amount
            else:
                self.large_sell += trade.amount
        if trade.price > self.high:
            self.high = trade.price
        if trade.price < self.low:
            self.low = trade.price
        # By trade time, not arrival: venues deliver slightly out of order and
        # replay on reconnect, so ordering by arrival would corrupt open/close.
        if trade.ts <= self.first_ts:
            self.first_ts, self.open = trade.ts, trade.price
        if trade.ts >= self.last_ts:
            self.last_ts, self.close = trade.ts, trade.price


class FlowBuffer:
    """Per-minute order flow, accumulated in memory from the live tape.

    Fed EVERY parsed trade, including those the settlement gate discards —
    that is the whole point. Storage then scales with how many markets we
    follow (1440 rows per market per day, flat) instead of with how much the
    market trades, which measurement showed is both far larger and wildly
    unpredictable: on Bybit, going from 5 to 30 symbols multiplied the row
    rate 5.6x, and a single high-churn listing outweighed ten majors.

    Only COMPLETED minutes are written. The in-flight bucket stays in memory,
    so a restart loses at most the current partial minute — writing it would
    persist a wrong number that nothing would ever correct, since the memory
    it would be completed from is gone.
    """

    def __init__(
        self,
        storage: Any,
        *,
        write: str = "write_trade_flow",
        grace_seconds: float = 5.0,
    ) -> None:
        self._storage = storage
        self._write_name = write
        # A bucket is sealed once the minute has ended plus this much slack,
        # covering venue clock skew and late-delivered trades.
        self._grace = grace_seconds
        self._buckets: dict[tuple, _Bucket] = {}
        self._lock = asyncio.Lock()
        self.written = 0

    def add(self, records: Sequence[Any]) -> None:
        for trade in records:
            minute = trade.ts.replace(second=0, microsecond=0)
            key = (trade.exchange, trade.market_type, trade.symbol, minute)
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = self._buckets[key] = _Bucket(trade)
            bucket.add(trade)

    def _sealed(self, now: datetime) -> list[tuple]:
        cutoff = now.timestamp() - 60 - self._grace
        return [k for k in self._buckets if k[3].timestamp() <= cutoff]

    async def flush(self, now: datetime | None = None) -> int:
        """Write every completed minute. Returns rows written."""
        from core.models import TradeFlow

        async with self._lock:
            now = now or datetime.now(timezone.utc)
            keys = self._sealed(now)
            if not keys:
                return 0
            records = []
            for key in keys:
                b = self._buckets.pop(key)
                exchange, market_type, symbol, minute = key
                records.append(TradeFlow(
                    exchange=exchange, market_type=market_type, symbol=symbol,
                    bucket=minute, trades=b.trades, volume=b.volume,
                    buy_volume=b.buy, sell_volume=b.sell, notional=b.notional,
                    large_trades=b.large, large_buy_volume=b.large_buy,
                    large_sell_volume=b.large_sell,
                    max_trade_notional=b.max_notional,
                    open=b.open, high=b.high, low=b.low, close=b.close,
                ))
            try:
                written = await getattr(self._storage, self._write_name)(records)
            except Exception:
                # Put them back so a database blip costs latency, not a hole in
                # the flow series — these are irreplaceable once the raw trades
                # they came from are gone.
                for record in records:
                    key = (record.exchange, record.market_type,
                           record.symbol, record.bucket)
                    self._buckets.setdefault(key, _replay(record))
                log.exception("%s: flow flush failed, %d bucket(s) requeued",
                              self._write_name, len(records))
                raise
            self.written += written
            return written

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=5.0)
                with contextlib.suppress(Exception):
                    await self.flush()
        finally:
            with contextlib.suppress(Exception):
                await self.flush()


def _replay(record) -> _Bucket:
    """Rebuild an accumulator from an already-finalised row, for requeueing
    after a failed flush."""
    class _Seed:
        ts, price = record.bucket, record.open
    bucket = _Bucket(_Seed())
    bucket.trades = record.trades
    bucket.volume, bucket.buy, bucket.sell = (
        record.volume, record.buy_volume, record.sell_volume)
    bucket.notional = record.notional
    bucket.large = record.large_trades
    bucket.large_buy, bucket.large_sell = (
        record.large_buy_volume, record.large_sell_volume)
    bucket.max_notional = record.max_trade_notional
    bucket.open, bucket.high = record.open, record.high
    bucket.low, bucket.close = record.low, record.close
    return bucket


class WriteBuffer:
    """Accumulate stream records, flush on size or age.

    `hard_cap` is a survival limit for the case where the database is slow or
    down while the feed keeps arriving. Past it, the OLDEST records are dropped
    and counted: unbounded growth would take the process out entirely, and a
    silent drop would be worse than a counted one — `dropped` rides along on
    every heartbeat so a shedding buffer shows up in the health page and in
    Discord rather than being discovered later as a hole in the data.
    """

    def __init__(
        self,
        storage: Any,
        write: str = "write_trades",
        *,
        max_rows: int = 1000,
        max_seconds: float = 2.0,
        hard_cap: int = 50_000,
    ) -> None:
        self._storage = storage
        self._write_name = write
        self._max_rows = max_rows
        self._max_seconds = max_seconds
        self._hard_cap = hard_cap

        self._pending: dict[Any, Any] = {}
        self._last_flush = time.monotonic()
        self._lock = asyncio.Lock()
        self.written = 0
        self.dropped = 0

    # -- sync side: called from the frame callback -------------------------------

    def add(self, records: Sequence[Any]) -> None:
        if not records:
            return
        for record in records:
            self._pending[record.key] = record
        overflow = len(self._pending) - self._hard_cap
        if overflow > 0:
            # dicts preserve insertion order, so the first keys are the oldest
            for key in list(self._pending)[:overflow]:
                del self._pending[key]
            self.dropped += overflow
            log.error(
                "%s: buffer over hard cap, dropped %d oldest records (%d total)",
                self._write_name, overflow, self.dropped,
            )

    @property
    def due(self) -> bool:
        return bool(self._pending) and (
            len(self._pending) >= self._max_rows
            or (time.monotonic() - self._last_flush) >= self._max_seconds
        )

    # -- async side --------------------------------------------------------------

    async def flush(self) -> int:
        """Write everything pending. Returns rows newly inserted.

        On failure the batch is put BACK, so a transient database blip costs
        latency rather than data — bounded by hard_cap, which is what stops
        that from becoming an unbounded leak if the outage persists.
        """
        async with self._lock:
            if not self._pending:
                self._last_flush = time.monotonic()
                return 0
            batch = list(self._pending.values())
            self._pending.clear()
            try:
                writer = getattr(self._storage, self._write_name)
                new_rows = await writer(batch)
            except Exception:
                for record in batch:
                    self._pending.setdefault(record.key, record)
                log.exception("%s: flush failed, %d records requeued",
                              self._write_name, len(batch))
                raise
            finally:
                self._last_flush = time.monotonic()
            self.written += new_rows
            return new_rows

    async def run(self, stop: asyncio.Event) -> None:
        """Flush whenever a batch comes due, until stopped — then flush once
        more so a clean SIGTERM never discards what is already in hand."""
        try:
            while not stop.is_set():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(), timeout=min(self._max_seconds, 1.0)
                    )
                if self.due:
                    with contextlib.suppress(Exception):
                        await self.flush()
        finally:
            with contextlib.suppress(Exception):
                await self.flush()
