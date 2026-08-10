
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import ClassVar

from core.enums import Exchange
from core.models import Trade
from data_collection.base import Capability


class BaseExchangeStream(ABC):
    exchange: ClassVar[Exchange]
    ws_url: ClassVar[str]
   
    capabilities: ClassVar[frozenset[Capability]] = frozenset()
    kind: ClassVar[str] = "stream"

    def __init__(self, symbols: Sequence[str] = (), venue: str | None = None) -> None:
        self.symbols = tuple(symbols)
        self.venue = venue or str(self.exchange)

    @property
    def stream_id(self) -> str:
        """Heartbeat key. Shares the job_runs keyspace with the polling jobs
        (`ohlcv:…`, `funding:…`), so the health page and the Discord alerts
        treat a stream exactly like any other job."""
        return f"stream:{self.venue}:{self.kind}"


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
