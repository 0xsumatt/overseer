from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import ClassVar

from core.enums import Exchange, MarketType, Timeframe
from core.models import OHLCV, FundingRate, LiquiditySnapshot
from data_collection.base import BaseExchangeScraper, Capability
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter

# Spec-enumerated interval strings; most match Timeframe values directly.
_INTERVALS: dict[Timeframe, str] = {
    Timeframe.M1: "1m", Timeframe.M3: "3m", Timeframe.M5: "5m",
    Timeframe.M15: "15m", Timeframe.M30: "30m", Timeframe.H1: "1h",
    Timeframe.H2: "2h", Timeframe.H4: "4h", Timeframe.H8: "8h",
    Timeframe.H12: "12h", Timeframe.D1: "1d",
}

_FUNDING_INTERVAL_HOURS = 8      # per the /stats schema ("Current 8-hour funding rate")


class BulkScraper(BaseExchangeScraper):
    exchange: ClassVar[Exchange] = Exchange.BULK
    # Spec's "Production" server. Confirm at mainnet launch; flip here if the
    # testnet/mainnet hosts differ.
    base_url: ClassVar[str] = "https://exchange-api.bulk.trade/api/v1"
    market_type: ClassVar[MarketType] = MarketType.PERP     # perp-only venue
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.OHLCV, Capability.FUNDING, Capability.LIQUIDITY}
    )

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_minute(300, burst=20),  # spec doesn't pin limits
            default_headers={"User-Agent": "overseer/0.1"},
        )

    # -- symbols: identity ("BTC-USD"); perp-only so market_type is fixed ----------

    def to_symbol(self, native: str) -> str:
        return native

    def to_native(self, symbol: str) -> str:
        return symbol

    # -- OHLCV: server-side resume via startTime ------------------------------------

    async def fetch_ohlcv(
        self, symbol: str, interval: Timeframe, since: datetime, *, limit: int = 1000
    ) -> Sequence[OHLCV]:
        rows = await self.http.get_json(
            f"{self.base_url}/klines",
            params={
                "symbol": symbol,
                "interval": _INTERVALS[interval],
                "startTime": str(self._to_ms(since)),
                "endTime": str(self._to_ms(datetime.now(timezone.utc))),
            },
        )
        out = [
            OHLCV(
                exchange=self.exchange,
                market_type=self.market_type,
                symbol=symbol,
                interval=interval,
                ts=self._from_ms(int(c["t"])),          # open time keys the bar
                open=self._dec(c["o"]), high=self._dec(c["h"]),
                low=self._dec(c["l"]), close=self._dec(c["c"]),
                volume=self._dec(c.get("v", 0)),        # base volume
            )
            for c in rows
        ]
        out.sort(key=lambda b: b.ts)                    # ordering unspecified: be safe
        return out

    # -- funding: SAMPLED current rate (no public history — see module docstring) ---

    async def fetch_funding(
        self, symbol: str, since: datetime
    ) -> Sequence[FundingRate]:
        tick = await self.http.get_json(f"{self.base_url}/ticker/{symbol}")
        rate = tick.get("fundingRate")
        if rate is None:
            return []
        # one snapshot per poll; ts = our clock (ticker's own ts is nanoseconds
        # of server time — our poll time is the honest label for a sample)
        return [
            FundingRate(
                exchange=self.exchange,
                symbol=symbol,
                ts=datetime.now(timezone.utc).replace(microsecond=0),
                rate=self._dec(rate),
                interval_hours=_FUNDING_INTERVAL_HOURS,
            )
        ]

    # -- liquidity: one ticker call per symbol ----------------------------------------

    async def fetch_liquidity(
        self, symbols: Sequence[str]
    ) -> Sequence[LiquiditySnapshot]:
        now = datetime.now(timezone.utc)
        out: list[LiquiditySnapshot] = []
        for symbol in symbols:
            tick = await self.http.get_json(f"{self.base_url}/ticker/{symbol}")
            out.append(
                LiquiditySnapshot(
                    exchange=self.exchange,
                    symbol=symbol,
                    ts=now,
                    open_interest=self._dec(tick.get("openInterest", 0)),   # base
                    volume_24h=self._dec(tick.get("quoteVolume", 0)),       # quote
                    mark_price=self._dec(tick.get("markPrice", 0)),
                )
            )
        return out