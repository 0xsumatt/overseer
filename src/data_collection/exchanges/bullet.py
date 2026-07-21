from __future__ import annotations

from typing import ClassVar

from core.enums import Exchange, MarketType
from data_collection.base import Capability
from data_collection.exchanges.binance import BinanceFuturesScraper
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter


class BulletScraper(BinanceFuturesScraper):
    exchange: ClassVar[Exchange] = Exchange.BULLET
    base_url: ClassVar[str] = "https://tradingapi.bullet.xyz"
    market_type: ClassVar[MarketType] = MarketType.PERP

    # verify on first live run — see module docstring
    DEFAULT_FUNDING_HOURS: ClassVar[int] = 8

    # -- symbols: hyphenated native format ("BTC-USD"), NOT Binance's
    #    concatenated-pair convention — see module docstring ---------------------

    def to_symbol(self, native: str) -> str:
        return native

    def to_native(self, symbol: str) -> str:
        return symbol

    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.OHLCV, Capability.FUNDING, Capability.LIQUIDITY,
         Capability.VENUE_VOLUME}
    )

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_minute(300, burst=20),   # unpublished limits
            default_headers={"User-Agent": "cryptodash/0.1"},
        )

    async def _funding_interval(self, native: str) -> int:
        """Binance-compat clones often omit /fapi/v1/fundingInfo; degrade to
        the venue default instead of failing every funding job."""
        if self._funding_intervals is None:
            try:
                info = await self.http.get_json(f"{self.base_url}/fapi/v1/fundingInfo")
                self._funding_intervals = {
                    x["symbol"]: int(x.get("fundingIntervalHours", self.DEFAULT_FUNDING_HOURS))
                    for x in info
                }
            except Exception:
                self._funding_intervals = {}     # endpoint absent: defaults for all
        return self._funding_intervals.get(native, self.DEFAULT_FUNDING_HOURS)