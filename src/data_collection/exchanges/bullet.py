from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import ClassVar

from core.enums import Exchange, MarketType, Timeframe
from core.models import LiquiditySnapshot, OHLCV
from data_collection.base import Capability, UnsupportedCapability
from data_collection.exchanges.binance import BinanceFuturesScraper
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter


class BulletScraper(BinanceFuturesScraper):
    exchange: ClassVar[Exchange] = Exchange.BULLET
    base_url: ClassVar[str] = "https://tradingapi.bullet.xyz"
    market_type: ClassVar[MarketType] = MarketType.PERP

    # verify on first live run — see module docstring
    DEFAULT_FUNDING_HOURS: ClassVar[int] = 8

    # Binance's weight units (_klines_weight/_ticker24h_weight) mean nothing
    # against Bullet's own limiter below — its real limits are unpublished, so
    # _build_http picked a flat conservative rate, not a weight-budget one.
    # Confirmed live: inheriting _ticker24h_weight=40 raised ValueError
    # outright (40 > this limiter's burst=20 — ratelimit.py refuses a request
    # that could never be admitted). Both reset to 1 token per call.
    _klines_weight: ClassVar[int] = 1
    _ticker24h_weight: ClassVar[int] = 1

    # -- symbols: hyphenated native format ("BTC-USD"), NOT Binance's
    #    concatenated-pair convention — see module docstring ---------------------

    def to_symbol(self, native: str) -> str:
        return native

    def to_native(self, symbol: str) -> str:
        return symbol

    # -- time: MICROSECOND epoch, not Binance's milliseconds — see module
    #    docstring. Same names as the base class on purpose: every inherited
    #    fetch_* method calls self._to_ms/self._from_ms, so overriding here
    #    redirects all of them via polymorphism with no other changes needed.

    @staticmethod
    def _to_ms(dt: datetime) -> int:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1_000_000)

    @staticmethod
    def _from_ms(us: int | float) -> datetime:
        return datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc)

    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.FUNDING, Capability.LIQUIDITY, Capability.VENUE_VOLUME}
    )

    # -- OHLCV: no klines endpoint exists on Bullet — see module docstring ------

    async def fetch_ohlcv(
        self, symbol: str, interval: Timeframe, since: datetime, *, limit: int = 1000
    ) -> Sequence[OHLCV]:
        raise UnsupportedCapability(self.exchange, "fetch_ohlcv")

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_minute(300, burst=20),   # unpublished limits
            default_headers={"User-Agent": "overseer/0.1"},
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

    # -- liquidity: openInterest/ticker ignore `symbol`, premiumIndex wraps a
    #    single match in a list — see module docstring. Fetch each once,
    #    index by symbol, instead of the inherited per-symbol Binance loop.

    async def fetch_liquidity(
        self, symbols: Sequence[str]
    ) -> Sequence[LiquiditySnapshot]:
        now = datetime.now(timezone.utc)
        oi_rows = await self.http.get_json(f"{self.base_url}/fapi/v1/openInterest")
        tick_rows = await self.http.get_json(f"{self.base_url}/fapi/v1/ticker/24hr")
        mark_rows = await self.http.get_json(f"{self.base_url}/fapi/v1/premiumIndex")
        oi_by = {r["symbol"]: r for r in oi_rows or []}
        tick_by = {r["symbol"]: r for r in tick_rows or []}
        mark_by = {r["symbol"]: r for r in mark_rows or []}

        out: list[LiquiditySnapshot] = []
        for symbol in symbols:
            native = self.to_native(symbol)
            oi, tick, mark = oi_by.get(native), tick_by.get(native), mark_by.get(native)
            if oi is None and tick is None and mark is None:
                continue        # not listed / not returned by any of the three
            out.append(
                LiquiditySnapshot(
                    exchange=self.exchange,
                    symbol=self.to_symbol(native),
                    ts=now,
                    open_interest=self._dec(oi["openInterest"]) if oi else self._dec(0),
                    volume_24h=self._dec(tick["quoteVolume"]) if tick else self._dec(0),
                    mark_price=self._dec(mark["markPrice"]) if mark else self._dec(0),
                )
            )
        return out