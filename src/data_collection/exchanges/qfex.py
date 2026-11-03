"""QFEX: perpetual futures on traditional assets (equities, indices,
commodities, FX), https://docs.qfex.com/api-reference/rest/market-data."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

from core.enums import Exchange, MarketType, QuoteCurrency, Timeframe
from core.models import OHLCV, FundingRate, LiquiditySnapshot, TopOfBook
from data_collection.base import BaseExchangeScraper, Capability, UnsupportedCapability
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter

_RESOLUTIONS: dict[Timeframe, str] = {
    Timeframe.M1: "1MIN", Timeframe.M5: "5MINS", Timeframe.M15: "15MINS",
    Timeframe.H1: "1HOUR", Timeframe.D1: "1DAY",
}
# The venue-volume window, as the nanosecond duration the endpoint expects.
_DAY_NS = 86_400 * 1_000_000_000


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class QfexScraper(BaseExchangeScraper):
    exchange: ClassVar[Exchange] = Exchange.QFEX
    base_url: ClassVar[str] = "https://api.qfex.com"
    market_type: ClassVar[MarketType] = MarketType.PERP
    # Prices are quoted in USD and margined in USDC, booked as USDC like Bullet's.
    quote_currency: ClassVar[QuoteCurrency] = QuoteCurrency.USDC
    capabilities: ClassVar[frozenset[Capability]] = frozenset({
        Capability.OHLCV, Capability.FUNDING, Capability.LIQUIDITY,
        Capability.BBO, Capability.VENUE_VOLUME,
    })

    # Every request pins a published contract, so a format change waits for a
    # deliberate upgrade: read https://docs.qfex.com/websocket/changelog first.
    API_VERSION: ClassVar[str] = "2026-10-30"
    # The funding history forward-fills gaps and stored settlements are never
    # rewritten, so a settlement counts once it is this old.
    SETTLED_AFTER: ClassVar[timedelta] = timedelta(minutes=5)

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_minute(120, burst=10),
            # Each top-of-book poll downloads the whole book; gzip makes it about
            # 5× smaller, and aiosonic decompresses responses but never asks.
            default_headers={"User-Agent": "overseer/0.1", "Accept-Encoding": "gzip"},
        )

    def to_symbol(self, native: str) -> str:
        return native

    def to_native(self, symbol: str) -> str:
        return symbol

    async def _get(self, path: str, **params: str) -> Any:
        return await self.http.get_json(
            f"{self.base_url}{path}", params={**params, "api_version": self.API_VERSION}
        )

    async def fetch_ohlcv(
        self, symbol: str, interval: Timeframe, since: datetime
    ) -> Sequence[OHLCV]:
        resolution = _RESOLUTIONS.get(interval)
        if resolution is None:
            raise UnsupportedCapability(self.exchange, f"fetch_ohlcv at {interval}")
        payload = await self._get(
            f"/candles/{self.to_native(symbol)}", resolution=resolution,
            fromISO=_iso(since), toISO=_iso(datetime.now(timezone.utc)),
        )
        bars = [
            OHLCV(
                exchange=self.exchange, market_type=self.market_type, symbol=symbol,
                interval=interval, ts=_parse(c["startedAt"]),
                open=self._dec(c["open"]), high=self._dec(c["high"]),
                low=self._dec(c["low"]), close=self._dec(c["close"]),
                volume=self._dec(c["baseTokenVolume"]),
            )
            for c in payload.get("candles") or []
        ]
        bars.sort(key=lambda bar: bar.ts)
        return bars

    async def fetch_funding(
        self, symbol: str, since: datetime
    ) -> Sequence[FundingRate]:
        """Settled hourly funding. Rates are fractions per settlement, stored
        as published (the trading page shows the same live rate); outside a
        market's trading hours they are zero."""
        now = datetime.now(timezone.utc)
        payload = await self._get(
            f"/funding/{self.to_native(symbol)}", intervalMinutes="60",
            fromISO=_iso(since), toISO=_iso(now),
        )
        out: list[FundingRate] = []
        for row in payload.get("data") or []:
            hours, rest = divmod(int(row["intervalMinutes"]), 60)
            if row.get("rate") is None or rest or not hours:
                continue
            # Despite its name, windowStart is when the rate was paid, for the
            # hour before: stocks' 01:00 UTC row is zero after Sunday's 20:00 ET
            # open, and Friday's last funded hour pays in the 00:00 UTC row.
            settled_at = _parse(row["windowStart"])
            if settled_at > now - self.SETTLED_AFTER:
                continue
            out.append(FundingRate(
                exchange=self.exchange, symbol=symbol, ts=settled_at,
                rate=self._dec(row["rate"]), interval_hours=hours,
            ))
        return out

    async def fetch_liquidity(
        self, symbols: Sequence[str]
    ) -> Sequence[LiquiditySnapshot]:
        payload = await self._get("/md/contracts")
        contracts = {c["ticker_id"]: c for c in payload.get("data") or []}
        now = datetime.now(timezone.utc)
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        out: list[LiquiditySnapshot] = []
        for symbol in symbols:
            contract = contracts.get(self.to_native(symbol))
            # Markets quoted in KRW, JPY and the like would put local-currency
            # volume beside USD volume everywhere else.
            if contract is None or contract["target_currency"] != "USD":
                continue
            hours, rest = divmod(int(contract["funding_interval_minutes"]), 60)
            whole_hours = hours > 0 and not rest
            index = self._dec(contract["index_price"])
            out.append(LiquiditySnapshot(
                exchange=self.exchange, symbol=symbol, ts=now,
                open_interest=self._dec(contract["open_interest"]),
                volume_24h=self._dec(contract["target_volume"]),
                # The REST API publishes no mark price, and QFEX values open
                # interest at the index (open_interest_usd = OI × index).
                mark_price=index,
                index_price=index,
                # The predicted rate for the settlement ending the current window.
                current_funding_rate=(
                    self._dec(contract["next_funding_rate"]) if whole_hours else None
                ),
                funding_interval_hours=hours if whole_hours else None,
                next_funding_at=next_hour if hours == 1 and not rest else None,
            ))
        return out

    async def fetch_bbo(self, symbol: str) -> TopOfBook:
        # The book is always the full depth and lists emptied levels as zero size.
        book = await self._get(f"/md/orderbook/{self.to_native(symbol)}")
        bids = [(self._dec(p), self._dec(q)) for p, q in book.get("bids") or [] if self._dec(q) > 0]
        asks = [(self._dec(p), self._dec(q)) for p, q in book.get("asks") or [] if self._dec(q) > 0]
        if not bids or not asks:
            raise RuntimeError(f"qfex returned an empty book for {symbol}")
        (bid_price, bid_size), (ask_price, ask_size) = max(bids), min(asks)
        return TopOfBook(
            exchange=self.exchange, symbol=symbol, quote_currency=self.quote_currency,
            ts=self._from_ms(book["timestamp"]),
            bid_price=bid_price, bid_size=bid_size, ask_price=ask_price, ask_size=ask_size,
        )

    async def fetch_venue_volume(self) -> dict:
        """The last complete UTC day's volume across every QFEX market."""
        now = datetime.now(timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        payload = await self._get(
            "/defillama/metrics", interval=str(_DAY_NS),
            fromISO=_iso(today - timedelta(days=2)), toISO=_iso(now),
        )
        days = [
            (_parse(row["windowStart"]), row) for row in payload.get("data") or []
            if _parse(row["windowStart"]) + timedelta(days=1) <= now
        ]
        if not days:
            raise RuntimeError("qfex returned no complete day of volume")
        _, latest = max(days, key=lambda day: day[0])
        return {"spot": None, "perp": self._dec(latest["dailyVolumeUSD"])}
