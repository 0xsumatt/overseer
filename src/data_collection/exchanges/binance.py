from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import ClassVar

from core.enums import Exchange, MarketType, Timeframe
from core.models import OHLCV
from data_collection.base import BaseExchangeScraper, Capability
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter

# Quote assets, longest first, so "BTCUSDT" splits on "USDT" not "USD".
_QUOTES: tuple[str, ...] = (
    "USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USD",
    "BTC", "ETH", "BNB", "EUR", "TRY", "GBP",
)


class BinanceSpotScraper(BaseExchangeScraper):
    exchange: ClassVar[Exchange] = Exchange.BINANCE
    base_url: ClassVar[str] = "https://api.binance.com"
    market_type: ClassVar[MarketType] = MarketType.SPOT
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.OHLCV})
    _klines_path: ClassVar[str] = "/api/v3/klines"
    _klines_weight: ClassVar[int] = 2

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_minute(6000, burst=120),   # 6000 weight/min/IP
            default_headers={"User-Agent": "overseer/0.1"},
        )

    # -- symbols ------------------------------------------------------------------

    def to_symbol(self, native: str) -> str:
        native = native.upper()
        for quote in _QUOTES:
            if native.endswith(quote) and len(native) > len(quote):
                return f"{native[: -len(quote)]}/{quote}"
        return native            # unknown quote — leave as-is rather than guess

    def to_native(self, symbol: str) -> str:
        return symbol.replace("/", "").upper()

    # -- OHLCV: GET klines (same shape spot vs fapi; path/weight via classvars) ----

    async def fetch_ohlcv(
        self, symbol: str, interval: Timeframe, since: datetime, *, limit: int = 1000
    ) -> Sequence[OHLCV]:
        rows = await self.http.get_json(
            f"{self.base_url}{self._klines_path}",
            params={
                "symbol": self.to_native(symbol),
                "interval": interval.value,
                "startTime": str(self._to_ms(since)),
                "limit": str(limit),
            },
            weight=self._klines_weight,
        )
        canonical = self.to_symbol(self.to_native(symbol))
        out: list[OHLCV] = []
        for r in rows:
            # [openTime, open, high, low, close, volume, closeTime, ...]
            out.append(
                OHLCV(
                    exchange=self.exchange,
                    market_type=self.market_type,
                    symbol=canonical,
                    interval=interval,
                    ts=self._from_ms(r[0]),
                    open=self._dec(r[1]),
                    high=self._dec(r[2]),
                    low=self._dec(r[3]),
                    close=self._dec(r[4]),
                    volume=self._dec(r[5]),
                )
            )
        return out


class BinanceFuturesScraper(BinanceSpotScraper):
    """USDT-M perpetual futures. Same shapes as spot; different host/path/limit."""

    base_url: ClassVar[str] = "https://fapi.binance.com"
    market_type: ClassVar[MarketType] = MarketType.PERP
    _klines_path: ClassVar[str] = "/fapi/v1/klines"
    _klines_weight: ClassVar[int] = 5            # fapi klines weight at limit <= 1000

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_minute(2400, burst=60),    # 2400 weight/min/IP (fapi)
            default_headers={"User-Agent": "overseer/0.1"},
        )