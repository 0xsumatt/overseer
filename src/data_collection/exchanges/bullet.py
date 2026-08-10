from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import ClassVar

from core.enums import Exchange, MarketType, QuoteCurrency, Timeframe
from core.models import LiquiditySnapshot, OHLCV, TopOfBook
from data_collection.base import Capability, UnsupportedCapability
from data_collection.exchanges.binance import BinanceFuturesScraper
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter


class BulletScraper(BinanceFuturesScraper):
    exchange: ClassVar[Exchange] = Exchange.BULLET
    base_url: ClassVar[str] = "https://tradingapi.bullet.xyz"
    market_type: ClassVar[MarketType] = MarketType.PERP
    quote_currency: ClassVar[QuoteCurrency] = QuoteCurrency.USDC

    DEFAULT_FUNDING_HOURS: ClassVar[int] = 1

    _klines_weight: ClassVar[int] = 1
    _ticker24h_weight: ClassVar[int] = 1


    def to_symbol(self, native: str) -> str:
        return native

    def to_native(self, symbol: str) -> str:
        return symbol

    @staticmethod
    def _to_ms(dt: datetime) -> int:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1_000_000)

    @staticmethod
    def _from_ms(us: int | float) -> datetime:
        return datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc)

    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {
            Capability.FUNDING,
            Capability.LIQUIDITY,
            Capability.VENUE_VOLUME,
            Capability.BBO,
        }
    )

    async def fetch_ohlcv(
        self, symbol: str, interval: Timeframe, since: datetime, *, limit: int = 1000
    ) -> Sequence[OHLCV]:
        raise UnsupportedCapability(self.exchange, "fetch_ohlcv")

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_minute(300, burst=20),  
            default_headers={"User-Agent": "overseer/0.1"},
        )

    async def _funding_interval(self, native: str) -> int:
        if self._funding_intervals is None:
            try:
                info = await self.http.get_json(f"{self.base_url}/fapi/v1/fundingInfo")
                self._funding_intervals = {
                    x["symbol"]: int(x.get("fundingIntervalHours", self.DEFAULT_FUNDING_HOURS))
                    for x in info
                }
            except Exception:
                self._funding_intervals = {}  
        return self._funding_intervals.get(native, self.DEFAULT_FUNDING_HOURS)

    async def fetch_bbo(self, symbol: str) -> TopOfBook:
        payload = await self.http.get_json(
            f"{self.base_url}/fapi/v1/depth",
            params={"symbol": self.to_native(symbol), "limit": "5"},
        )
        bids, asks = payload.get("bids", []), payload.get("asks", [])
        if not bids or not asks:
            raise RuntimeError(f"bullet returned an empty book for {symbol}")
        bid = max(bids, key=lambda row: self._dec(row[0]))
        ask = min(asks, key=lambda row: self._dec(row[0]))
        return TopOfBook(
            exchange=self.exchange,
            symbol=symbol,
            quote_currency=self.quote_currency,
            ts=datetime.now(timezone.utc),
            bid_price=self._dec(bid[0]),
            bid_size=self._dec(bid[1]),
            ask_price=self._dec(ask[0]),
            ask_size=self._dec(ask[1]),
        )

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
                continue
            current_rate = mark.get("estimatedFundingRate") if mark else None
            if current_rate is None and mark:
                current_rate = mark.get("lastFundingRate")
            next_funding = mark.get("nextFundingTime") if mark else None
            hours = await self._funding_interval(native)
            out.append(
                LiquiditySnapshot(
                    exchange=self.exchange,
                    symbol=self.to_symbol(native),
                    ts=now,
                    open_interest=self._dec(oi["openInterest"]) if oi else self._dec(0),
                    volume_24h=self._dec(tick["quoteVolume"]) if tick else self._dec(0),
                    mark_price=self._dec(mark["markPrice"]) if mark else self._dec(0),
                    index_price=self._dec(mark["indexPrice"]) if mark else None,
                    current_funding_rate=(
                        self._dec(current_rate) if current_rate is not None else None
                    ),
                    funding_interval_hours=hours,
                    next_funding_at=(
                        datetime.fromtimestamp(int(next_funding) / 1000, tz=timezone.utc)
                        if next_funding else None
                    ),
                )
            )
        return out