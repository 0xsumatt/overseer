from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, ClassVar

from core.enums import Exchange, MarketType, QuoteCurrency, Timeframe
from core.models import FundingRate, LiquiditySnapshot, OHLCV, TopOfBook
from data_collection.base import BaseExchangeScraper, Capability
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter

_QUOTES: tuple[str, ...] = (
    "USDT", "USDC", "FDUSD", "TUSD", "DAI", "USD",
    "BTC", "ETH", "EUR", "TRY", "GBP", "BRL",
)

_INTERVALS: dict[Timeframe, str] = {
    Timeframe.M1: "1", Timeframe.M3: "3", Timeframe.M5: "5",
    Timeframe.M15: "15", Timeframe.M30: "30", Timeframe.H1: "60",
    Timeframe.H2: "120", Timeframe.H4: "240", Timeframe.H8: "480",
    Timeframe.H12: "720", Timeframe.D1: "D",
}



def _window_ms(since: datetime, max_lookback_days: int = 60) -> tuple[int, int]:

    now = datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)
    floor = end_ms - max_lookback_days * 86_400_000
    start_ms = int(since.timestamp() * 1000)
    start_ms = max(start_ms, floor)         
    if start_ms >= end_ms:
        start_ms = end_ms - 60_000           
    return start_ms, end_ms


_RATE_LIMIT_CODES = {10006, 10018}    
_RATE_LIMIT_BACKOFF = 30.0             





def to_canonical(native: str) -> str:
    native = native.upper()
    for quote in _QUOTES:
        if native.endswith(quote) and len(native) > len(quote):
            return f"{native[: -len(quote)]}/{quote}"
    return native


class BybitSpotScraper(BaseExchangeScraper):
    exchange: ClassVar[Exchange] = Exchange.BYBIT
    base_url: ClassVar[str] = "https://api.bybit.com"
    market_type: ClassVar[MarketType] = MarketType.SPOT
    category: ClassVar[str] = "spot"
    quote_currency: ClassVar[QuoteCurrency] = QuoteCurrency.USDT
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.OHLCV, Capability.VENUE_VOLUME, Capability.BBO}
    )

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_second(5, burst=10),
            default_headers={"User-Agent": "overseer/0.1"},
        )


    def _unwrap(self, payload: Any) -> Any:
        code = payload.get("retCode")
        if code == 0:
            return payload["result"]
        if code in _RATE_LIMIT_CODES:
            self.http.pause(_RATE_LIMIT_BACKOFF)
        raise RuntimeError(f"bybit error {code}: {payload.get('retMsg')}")

    def to_symbol(self, native: str) -> str:
        return to_canonical(native)

    def to_native(self, symbol: str) -> str:
        return symbol.replace("/", "").upper()

    async def fetch_bbo(self, symbol: str) -> TopOfBook:
        native = self.to_native(symbol)
        payload = await self.http.get_json(
            f"{self.base_url}/v5/market/tickers",
            params={"category": self.category, "symbol": native},
        )
        rows = self._unwrap(payload)["list"]
        if not rows:
            raise RuntimeError(f"bybit returned no BBO for {native}")
        row = rows[0]
        return TopOfBook(
            exchange=self.exchange,
            symbol=self.to_symbol(native),
            quote_currency=self.quote_currency,
            ts=datetime.now(timezone.utc),
            bid_price=self._dec(row["bid1Price"]),
            bid_size=self._dec(row["bid1Size"]),
            ask_price=self._dec(row["ask1Price"]),
            ask_size=self._dec(row["ask1Size"]),
        )


    async def fetch_ohlcv(
        self, symbol: str, interval: Timeframe, since: datetime, *, limit: int = 1000
    ) -> Sequence[OHLCV]:
        start_ms, end_ms = _window_ms(since)
        payload = await self.http.get_json(
            f"{self.base_url}/v5/market/kline",
            params={
                "category": self.category,
                "symbol": self.to_native(symbol),
                "interval": _INTERVALS[interval],
                "start": str(start_ms),
                "end": str(end_ms),
                "limit": str(limit),
            },
        )
        rows = self._unwrap(payload)["list"]
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
                volume=self._dec(r[5]),                   
            )
            for r in rows
        ]
        out.reverse()         
        return out


    async def fetch_venue_volume(self) -> dict:
        payload = await self.http.get_json(
            f"{self.base_url}/v5/market/tickers", params={"category": self.category}
        )
        items = self._unwrap(payload)["list"]
        total = sum(self._dec(t["turnover24h"]) for t in items) if items else None
        return {"spot": total, "perp": None} if self.market_type is MarketType.SPOT \
            else {"spot": None, "perp": total}


class BybitPerpScraper(BybitSpotScraper):
   

    market_type: ClassVar[MarketType] = MarketType.PERP
    category: ClassVar[str] = "linear"
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {
            Capability.OHLCV,
            Capability.FUNDING,
            Capability.LIQUIDITY,
            Capability.VENUE_VOLUME,
            Capability.BBO,
        }
    )

    _funding_intervals: dict[str, int] | None = None   

    async def _funding_interval(self, native: str) -> int:
        if self._funding_intervals is None:
            self._funding_intervals = {}
        if native not in self._funding_intervals:
            payload = await self.http.get_json(
                f"{self.base_url}/v5/market/instruments-info",
                params={"category": "linear", "symbol": native},
            )
            items = self._unwrap(payload)["list"]
            minutes = int(items[0]["fundingInterval"]) if items else 480
            self._funding_intervals[native] = max(1, minutes // 60)
        return self._funding_intervals[native]

    async def fetch_funding(
        self, symbol: str, since: datetime, *, limit: int = 200
    ) -> Sequence[FundingRate]:
        native = self.to_native(symbol)
        start_ms, end_ms = _window_ms(since)
        payload = await self.http.get_json(
            f"{self.base_url}/v5/market/funding/history",
            params={
                "category": "linear",
                "symbol": native,
                "startTime": str(start_ms),
                "endTime": str(end_ms),            
                "limit": str(limit),               
            },
        )
        rows = self._unwrap(payload)["list"]
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
        out.reverse()         
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
            items = self._unwrap(payload)["list"]
            if not items:
                continue
            t = items[0]
            hours = int(
                t.get("fundingIntervalHour")
                or await self._funding_interval(t["symbol"])
            )
            next_funding = t.get("nextFundingTime")
            current_rate = t.get("fundingRate")
            out.append(
                LiquiditySnapshot(
                    exchange=self.exchange,
                    symbol=self.to_symbol(t["symbol"]),
                    ts=now,
                    open_interest=self._dec(t["openInterest"]),
                    volume_24h=self._dec(t["turnover24h"]),
                    mark_price=self._dec(t["markPrice"]),
                    index_price=self._dec(t["indexPrice"]),
                    current_funding_rate=(
                        self._dec(current_rate) if current_rate not in (None, "") else None
                    ),
                    funding_interval_hours=hours,
                    next_funding_at=(
                        self._from_ms(int(next_funding)) if next_funding else None
                    ),
                )
            )
        return out