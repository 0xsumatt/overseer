

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

from core.enums import Exchange, MarketType, Timeframe
from core.models import OHLCV, Trade
from data_collection.http import HttpClient


class Capability(StrEnum):
    OHLCV = "ohlcv"
    TRADES = "trades"      # public tape — a STREAM (websocket) capability, not REST
    FILLS = "fills"        # per-address fills; on-demand utility, not scheduled


class UnsupportedCapability(NotImplementedError):
    def __init__(self, exchange: Exchange, method: str) -> None:
        super().__init__(f"{exchange} does not support {method} over REST")
        self.exchange = exchange
        self.method = method


class BaseExchangeScraper(ABC):
    # -- venue identity & config (set by each adapter) ----------------------------
    exchange: ClassVar[Exchange]
    base_url: ClassVar[str]
    market_type: ClassVar[MarketType]          # the market this adapter covers
    capabilities: ClassVar[frozenset[Capability]] = frozenset()

    def __init__(self, http: HttpClient | None = None) -> None:
        # injectable for tests; otherwise the venue builds its own client with
        # the right limiter and headers.
        self.http = http if http is not None else self._build_http()

    @abstractmethod
    def _build_http(self) -> HttpClient:
        """Construct the venue's rate-limited HttpClient."""

    # -- symbol translation (each venue knows its own convention) -----------------

    @abstractmethod
    def to_symbol(self, native: str) -> str:
        """Native venue symbol -> canonical symbol (e.g. 'BTCUSDT' -> 'BTC/USDT')."""

    @abstractmethod
    def to_native(self, symbol: str) -> str:
        """Canonical symbol -> native venue symbol for building requests."""

    def market_type_for(self, symbol: str) -> MarketType:
        """Market type for a canonical symbol — needed before a fetch to look up
        the right resume point. Fixed per adapter by default; venues that mix
        markets through one endpoint (Hyperliquid) override to derive it."""
        return self.market_type

    # -- data methods: default to "unsupported"; venues override what they serve --

    async def fetch_ohlcv(
        self, symbol: str, interval: Timeframe, since: datetime
    ) -> Sequence[OHLCV]:
        raise UnsupportedCapability(self.exchange, "fetch_ohlcv")

    async def fetch_fills(self, address: str, since: datetime) -> Sequence[Trade]:
        """Per-address fills — the path that carries a wallet_address."""
        raise UnsupportedCapability(self.exchange, "fetch_fills")

    # -- lifecycle ----------------------------------------------------------------

    async def aclose(self) -> None:
        await self.http.aclose()

    async def __aenter__(self) -> "BaseExchangeScraper":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()



    @staticmethod
    def _to_ms(dt: datetime) -> int:
        """tz-aware datetime -> epoch milliseconds (what both venues expect)."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    @staticmethod
    def _from_ms(ms: int | float) -> datetime:
        """epoch milliseconds -> tz-aware UTC datetime."""
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

    @staticmethod
    def _dec(value: str | int | float) -> Decimal:
        """Parse a venue number to Decimal. Always go via str so float noise
        ('0.1' + '0.2' style) never enters price/size data."""
        return Decimal(str(value))