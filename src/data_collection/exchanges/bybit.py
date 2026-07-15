from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, ClassVar

from core.enums import Exchange, MarketType, Timeframe
from core.models import OHLCV, FundingRate, LiquiditySnapshot
from data_collection.base import BaseExchangeScraper, Capability
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter

_QUOTES: tuple[str, ...] = (
    "USDT", "USDC", "FDUSD", "TUSD", "DAI", "USD",
    "BTC", "ETH", "EUR", "TRY", "GBP", "BRL",
)

# Timeframe -> bybit interval string ("1","5","60","240","D"…)
_INTERVALS: dict[Timeframe, str] = {
    Timeframe.M1: "1", Timeframe.M3: "3", Timeframe.M5: "5",
    Timeframe.M15: "15", Timeframe.M30: "30", Timeframe.H1: "60",
    Timeframe.H2: "120", Timeframe.H4: "240", Timeframe.H8: "480",
    Timeframe.H12: "720", Timeframe.D1: "D",
}


def _unwrap(payload: Any) -> Any:
    """v5 envelope: HTTP 200 with retCode != 0 is still an error."""
    if payload.get("retCode") != 0:
        raise RuntimeError(f"bybit error {payload.get('retCode')}: {payload.get('retMsg')}")
    return payload["result"]


class BybitSpotScraper(BaseExchangeScraper):
    exchange: ClassVar[Exchange] = Exchange.BYBIT
    base_url: ClassVar[str] = "https://api.bybit.com"
    market_type: ClassVar[MarketType] = MarketType.SPOT
    category: ClassVar[str] = "spot"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.OHLCV})

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_second(20, burst=40),
            default_headers={"User-Agent": "overseer/0.1"},
        )

    # -- symbols (BTCUSDT concatenated, like Binance) ------------------------------

    def to_symbol(self, native: str) -> str:
        native = native.upper()
        for quote in _QUOTES:
            if native.endswith(quote) and len(native) > len(quote):
                return f"{native[: -len(quote)]}/{quote}"
        return native

    def to_native(self, symbol: str) -> str:
        return symbol.replace("/", "").upper()

    # -- OHLCV ----------------------------------------------------------------------

    async def fetch_ohlcv(
        self, symbol: str, interval: Timeframe, since: datetime, *, limit: int = 1000
    ) -> Sequence[OHLCV]:
        payload = await self.http.get_json(
            f"{self.base_url}/v5/market/kline",
            params={
                "category": self.category,
                "symbol": self.to_native(symbol),
                "interval": _INTERVALS[interval],
                "start": str(self._to_ms(since)),
                "limit": str(limit),
            },
        )
        rows = _unwrap(payload)["list"]
        canonical = self.to_symbol(self.to_native(symbol))
        out = [
            OHLCV(
                exchange=self.exchange,
                market_type=self.market_type,
                symbol=canonical,
                interval=interval,
                ts=self._from_ms(int(r[0])),
                open=self._dec(r[1]), high=self._dec(r[2]),
                low=self._dec(r[3]), close=self._dec(r[4]),
                volume=self._dec(r[5]),                    # base volume
            )
            for r in rows
        ]
        out.reverse()          # bybit returns newest-first
        return out


class BybitPerpScraper(BybitSpotScraper):
    """USDT linear perpetuals: same endpoints, category=linear, plus funding
    and liquidity (perp-domain feeds)."""

    market_type: ClassVar[MarketType] = MarketType.PERP
    category: ClassVar[str] = "linear"
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.OHLCV, Capability.FUNDING, Capability.LIQUIDITY}
    )

    _funding_intervals: dict[str, int] | None = None   # native -> hours

    async def _funding_interval(self, native: str) -> int:
        if self._funding_intervals is None:
            self._funding_intervals = {}
        if native not in self._funding_intervals:
            payload = await self.http.get_json(
                f"{self.base_url}/v5/market/instruments-info",
                params={"category": "linear", "symbol": native},
            )
            items = _unwrap(payload)["list"]
            minutes = int(items[0]["fundingInterval"]) if items else 480
            self._funding_intervals[native] = max(1, minutes // 60)
        return self._funding_intervals[native]

    async def fetch_funding(
        self, symbol: str, since: datetime, *, limit: int = 200
    ) -> Sequence[FundingRate]:
        native = self.to_native(symbol)
        payload = await self.http.get_json(
            f"{self.base_url}/v5/market/funding/history",
            params={
                "category": "linear",
                "symbol": native,
                "startTime": str(self._to_ms(since)),
                "limit": str(limit),               # bybit caps this at 200
            },
        )
        rows = _unwrap(payload)["list"]
        hours = await self._funding_interval(native)
        canonical = self.to_symbol(native)
        out = [
            FundingRate(
                exchange=self.exchange,
                symbol=canonical,
                ts=self._from_ms(int(r["fundingRateTimestamp"])),
                rate=self._dec(r["fundingRate"]),
                interval_hours=hours,
            )
            for r in rows
        ]
        out.reverse()          # newest-first from the API
        return out

    async def fetch_liquidity(
        self, symbols: Sequence[str]
    ) -> Sequence[LiquiditySnapshot]:
        now = datetime.now(timezone.utc)
        out: list[LiquiditySnapshot] = []
        for symbol in symbols:
            payload = await self.http.get_json(
                f"{self.base_url}/v5/market/tickers",
                params={"category": "linear", "symbol": self.to_native(symbol)},
            )
            items = _unwrap(payload)["list"]
            if not items:
                continue
            t = items[0]
            out.append(
                LiquiditySnapshot(
                    exchange=self.exchange,
                    symbol=self.to_symbol(t["symbol"]),
                    ts=now,
                    open_interest=self._dec(t["openInterest"]),    # base units
                    volume_24h=self._dec(t["turnover24h"]),        # quote notional
                    mark_price=self._dec(t["markPrice"]),
                )
            )
        return out