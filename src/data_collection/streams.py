"""Venue stream adapters — the websocket sibling of BaseExchangeScraper.

Deliberately a separate base class rather than more methods on the scraper.
BaseExchangeScraper's entire contract is "call it, get a list back, it holds no
state between calls"; a stream is a long-lived connection with a lifecycle, a
subscription set and health of its own. Bolting one onto the other would make
both harder to reason about, and the two are wired up by different machinery
anyway (APScheduler polls scrapers; scheduler/streams.py supervises streams).

A stream adapter is intentionally tiny — it owns exactly two decisions:

    subscribe_frames()  what to say on connect (re-sent on every reconnect)
    parse(payload)      bytes -> domain records

Everything else — connecting, reconnecting with backoff, ping/pong, liveness
accounting, buffering, database writes, health heartbeats — belongs to
data_collection/ws.py and storage/buffer.py. If a venue adapter grows a
connection concern, it is in the wrong file.

`parse` is called from picows' SYNCHRONOUS frame callback, so implementations
must be fast and must not await, block or touch the database. Use orjson
(already a project dependency) on the raw bytes rather than decoding to str
first — orjson.loads takes bytes directly.

Scope note: `parse` returns Trades, which covers the tape and per-account fills
— the two feeds that write to the existing `trades` table. Order-book depth is
deliberately NOT modelled here. A book is materialised state maintained from a
snapshot plus sequenced deltas, needing gap detection and resync, and it is not
append-only, so it wants its own base class and its own sink rather than being
forced through a records-to-rows path. See the note in scheduler/streams.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import ClassVar

from core.enums import Exchange
from core.models import Trade
from data_collection.base import Capability


class BaseExchangeStream(ABC):
    # -- venue identity (set by each adapter) -------------------------------------
    exchange: ClassVar[Exchange]
    ws_url: ClassVar[str]
    # What this stream carries: Capability.TRADES (public tape) or
    # Capability.FILLS (per-account). Both already exist on the REST side and
    # are documented there as stream capabilities.
    capabilities: ClassVar[frozenset[Capability]] = frozenset()
    # Short tag distinguishing two streams on one venue, e.g. "trades", "fills".
    kind: ClassVar[str] = "stream"

    def __init__(self, symbols: Sequence[str] = (), venue: str | None = None) -> None:
        self.symbols = tuple(symbols)
        # The VENUE id, not the exchange: one exchange can be several venues
        # (binance_spot and binance_perp are both Exchange.BINANCE), and their
        # heartbeats must not collide in job_runs. Defaults to the exchange
        # name, which is correct for single-venue exchanges like Hyperliquid.
        self.venue = venue or str(self.exchange)

    @property
    def stream_id(self) -> str:
        """Heartbeat key. Shares the job_runs keyspace with the polling jobs
        (`ohlcv:…`, `funding:…`), so the health page and the Discord alerts
        treat a stream exactly like any other job."""
        return f"stream:{self.venue}:{self.kind}"

    # -- the two decisions an adapter owns ------------------------------------------

    @abstractmethod
    def subscribe_frames(self) -> Sequence[bytes]:
        """Frames to send on every fresh connection.

        Called per-connect rather than once, so an adapter needing a nonce or
        a timestamp in its subscribe payload can build one; a reconnect is
        otherwise invisible to the adapter.
        """

    # Some venues require an APPLICATION-level keepalive on top of the
    # websocket protocol's own ping/pong — Bybit wants {"op":"ping"} every 20s,
    # for instance — and close the socket without one. picows' auto-ping covers
    # the protocol level only, so this is the venue's hook. Leave as None where
    # the protocol ping suffices (Hyperliquid documents no app-level ping).
    keepalive_seconds: ClassVar[float | None] = None

    def keepalive_frames(self) -> Sequence[bytes]:
        """Frames to send every `keepalive_seconds` while connected."""
        return ()

    @abstractmethod
    def parse(self, payload: bytes) -> Sequence[Trade]:
        """One raw data frame -> zero or more Trades.

        Returning empty is normal and expected: venues interleave
        subscription acknowledgements, heartbeats and errors on the same
        socket. Raise only on genuinely malformed input — the caller counts
        the error, logs it and keeps the connection up, because one bad frame
        must not cause a reconnect loop.
        """
