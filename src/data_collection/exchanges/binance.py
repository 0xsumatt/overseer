from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import ClassVar

from core.enums import Exchange, MarketType, QuoteCurrency, Timeframe
from core.models import FundingRate, LiquiditySnapshot, OHLCV, TopOfBook
from data_collection.base import BaseExchangeScraper, Capability
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter


_QUOTES: tuple[str, ...] = (
    "USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USD",
    "BTC", "ETH", "BNB", "EUR", "TRY", "GBP",
)

def to_canonical(native: str) -> str:
    native = native.upper()
    for quote in _QUOTES:
        if native.endswith(quote) and len(native) > len(quote):
            return f"{native[: -len(quote)]}/{quote}"
    return native            


class BinanceSpotScraper(BaseExchangeScraper):
    exchange: ClassVar[Exchange] = Exchange.BINANCE
    base_url: ClassVar[str] = "https://api.binance.com"
    market_type: ClassVar[MarketType] = MarketType.SPOT
    quote_currency: ClassVar[QuoteCurrency] = QuoteCurrency.USDT
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.OHLCV, Capability.VENUE_VOLUME, Capability.BBO}
    )
    _klines_path: ClassVar[str] = "/api/v3/klines"
    _klines_weight: ClassVar[int] = 2
    _ticker24h_path: ClassVar[str] = "/api/v3/ticker/24hr"
    _ticker24h_weight: ClassVar[int] = 80       
    _book_ticker_path: ClassVar[str] = "/api/v3/ticker/bookTicker"

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_minute(6000, burst=120),  
            default_headers={"User-Agent": "overseer/0.1"},
        )


    def to_symbol(self, native: str) -> str:
        return to_canonical(native)

    def to_native(self, symbol: str) -> str:
        return symbol.replace("/", "").upper()

    async def fetch_bbo(self, symbol: str) -> TopOfBook:
        native = self.to_native(symbol)
        row = await self.http.get_json(
            f"{self.base_url}{self._book_ticker_path}", params={"symbol": native}
        )
        return TopOfBook(
            exchange=self.exchange,
            symbol=self.to_symbol(native),
            quote_currency=self.quote_currency,
            ts=datetime.now(timezone.utc),
            bid_price=self._dec(row["bidPrice"]),
            bid_size=self._dec(row["bidQty"]),
            ask_price=self._dec(row["askPrice"]),
            ask_size=self._dec(row["askQty"]),
        )


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

    async def fetch_venue_volume(self) -> dict:
        rows = await self.http.get_json(
            f"{self.base_url}{self._ticker24h_path}", weight=self._ticker24h_weight
        )
        total = sum(self._dec(r["quoteVolume"]) for r in rows) if rows else None
        return {"spot": total, "perp": None} if self.market_type is MarketType.SPOT \
            else {"spot": None, "perp": total}


class BinanceFuturesScraper(BinanceSpotScraper):

    base_url: ClassVar[str] = "https://fapi.binance.com"
    market_type: ClassVar[MarketType] = MarketType.PERP
    _klines_path: ClassVar[str] = "/fapi/v1/klines"
    _klines_weight: ClassVar[int] = 5            
    _ticker24h_path: ClassVar[str] = "/fapi/v1/ticker/24hr"
    _ticker24h_weight: ClassVar[int] = 40         
    _book_ticker_path: ClassVar[str] = "/fapi/v1/ticker/bookTicker"

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_minute(2400, burst=60),    
            default_headers={"User-Agent": "overseer/0.1"},
        )


    capabilities = frozenset(
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
            info = await self.http.get_json(f"{self.base_url}/fapi/v1/fundingInfo")
            self._funding_intervals = {
                i["symbol"]: int(i["fundingIntervalHours"]) for i in info
            }
        return self._funding_intervals.get(native, 8)

    async def fetch_funding(
        self, symbol: str, since: datetime, *, limit: int = 1000
    ) -> Sequence[FundingRate]:
        native = self.to_native(symbol)
        rows = await self.http.get_json(
            f"{self.base_url}/fapi/v1/fundingRate",
            params={
                "symbol": native,
                "startTime": str(self._to_ms(since)),
                "limit": str(limit),
            },
        )
        hours = await self._funding_interval(native)
        canonical = self.to_symbol(native)
        return [
            FundingRate(
                exchange=self.exchange,
                symbol=canonical,
                ts=self._from_ms(r["fundingTime"]),
                rate=self._dec(r["fundingRate"]),
                interval_hours=hours,
            )
            for r in rows
        ]


    async def fetch_liquidity(
        self, symbols: Sequence[str]
    ) -> Sequence[LiquiditySnapshot]:
        from datetime import datetime as _dt, timezone as _tz
        out: list[LiquiditySnapshot] = []
        now = _dt.now(_tz.utc)
        for symbol in symbols:
            native = self.to_native(symbol)
            oi = await self.http.get_json(
                f"{self.base_url}/fapi/v1/openInterest", params={"symbol": native}
            )
            tick = await self.http.get_json(
                f"{self.base_url}/fapi/v1/ticker/24hr", params={"symbol": native}
            )
            mark = await self.http.get_json(
                f"{self.base_url}/fapi/v1/premiumIndex", params={"symbol": native}
            )
            hours = await self._funding_interval(native)
            next_funding = mark.get("nextFundingTime")
            current_rate = mark.get("lastFundingRate")
            out.append(
                LiquiditySnapshot(
                    exchange=self.exchange,
                    symbol=self.to_symbol(native),
                    ts=now,
                    open_interest=self._dec(oi["openInterest"]),
                    volume_24h=self._dec(tick["quoteVolume"]),
                    mark_price=self._dec(mark["markPrice"]),
                    index_price=self._dec(mark["indexPrice"]),
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