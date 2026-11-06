"""Phoenix: Solana perpetual futures on crypto, equities, ETFs, commodities
and pre-IPO names, https://docs.phoenix.trade/api. Public market data needs
no auth. Live market state comes from the WebSocket, as Phoenix recommends
(https://docs.phoenix.trade/sdk/ws-api-best-practices); history and books
from REST."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, ClassVar

import orjson
from picows import WSFrame, WSListener, WSMsgType, WSTransport, ws_connect

from core.enums import Exchange, MarketType, QuoteCurrency, Timeframe
from core.models import OHLCV, FundingRate, LiquiditySnapshot, TopOfBook
from data_collection.base import BaseExchangeScraper, Capability, UnsupportedCapability
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter

log = logging.getLogger("overseer.phoenix")

# Candle timeframes Phoenix serves, by length. A request may span at most
# 2,500 candles, so longer ranges are paged.
_BAR_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60, Timeframe.M5: 300, Timeframe.M15: 900, Timeframe.M30: 1800,
    Timeframe.H1: 3600, Timeframe.H4: 4 * 3600, Timeframe.D1: 86_400,
}
_MAX_CANDLES = 2500


def _ms(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1000))


class _MarketStatsListener(WSListener):
    """Resolves `done` with symbol -> its first `market` stats message once
    every symbol has sent one, or with whatever arrived if the socket closes."""

    def __init__(self, symbols: Sequence[str], done: asyncio.Future) -> None:
        self.symbols = list(dict.fromkeys(symbols))
        self.done = done
        self.stats: dict[str, dict] = {}

    def on_ws_connected(self, transport: WSTransport) -> None:
        for symbol in self.symbols:
            transport.send(WSMsgType.TEXT, orjson.dumps({
                "type": "subscribe", "subscription": {"channel": "market", "symbol": symbol},
            }))

    def on_ws_frame(self, transport: WSTransport, frame: WSFrame) -> None:
        if frame.msg_type != WSMsgType.TEXT or self.done.done():
            return
        message = orjson.loads(frame.get_payload_as_bytes())
        if message.get("channel") == "market":
            self.stats.setdefault(message["symbol"], message)
            if len(self.stats) >= len(self.symbols):
                self.done.set_result(self.stats)

    def on_ws_disconnected(self, transport: WSTransport) -> None:
        if not self.done.done():
            self.done.set_result(self.stats)


class PhoenixScraper(BaseExchangeScraper):
    exchange: ClassVar[Exchange] = Exchange.PHOENIX
    base_url: ClassVar[str] = "https://perp-api.phoenix.trade"
    market_type: ClassVar[MarketType] = MarketType.PERP
    quote_currency: ClassVar[QuoteCurrency] = QuoteCurrency.USDC
    capabilities: ClassVar[frozenset[Capability]] = frozenset({
        Capability.OHLCV, Capability.FUNDING, Capability.LIQUIDITY,
        Capability.BBO, Capability.VENUE_VOLUME,
    })

    WS_URL: ClassVar[str] = "wss://perp-api.phoenix.trade/v1/ws"
    # Each subscribed market sends its stats within a few seconds.
    STATS_TIMEOUT: ClassVar[float] = 15.0

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_minute(300, burst=20),
            default_headers={"User-Agent": "overseer/0.1"},
        )

    def to_symbol(self, native: str) -> str:
        return native

    def to_native(self, symbol: str) -> str:
        return symbol

    async def _get(self, path: str, **params: str) -> Any:
        return await self.http.get_json(f"{self.base_url}{path}", params=params or None)

    async def _market_stats(self, symbols: Sequence[str]) -> dict[str, dict]:
        """symbol -> live stats (open interest, mark, oracle, 24h notional
        volume, current funding), one `market` message each. Markets silent
        past the timeout are left out rather than failing the rest."""
        done = asyncio.get_running_loop().create_future()
        listener = _MarketStatsListener([self.to_native(s) for s in symbols], done)
        transport: WSTransport | None = None
        try:
            transport, _ = await asyncio.wait_for(
                ws_connect(lambda: listener, self.WS_URL), self.STATS_TIMEOUT
            )
            return await asyncio.wait_for(done, self.STATS_TIMEOUT)
        except TimeoutError:
            missing = sorted(set(listener.symbols) - set(listener.stats))
            log.warning("phoenix: no market stats for %s", ", ".join(missing) or "any market")
            return dict(listener.stats)
        finally:
            if transport is not None:
                transport.disconnect()

    async def fetch_ohlcv(
        self, symbol: str, interval: Timeframe, since: datetime
    ) -> Sequence[OHLCV]:
        step = _BAR_SECONDS.get(interval)
        if step is None:
            raise UnsupportedCapability(self.exchange, f"fetch_ohlcv({interval})")
        now = datetime.now(timezone.utc)
        bars: dict[datetime, OHLCV] = {}
        start = since
        while start < now:
            end = min(start + timedelta(seconds=step * (_MAX_CANDLES - 1)), now)
            rows = await self._get(
                f"/v1/candles/{self.to_native(symbol)}", timeframe=interval.value,
                startTime=_ms(start), endTime=_ms(end),
            )
            for row in rows:
                ts = self._from_ms(row["time"])
                bars[ts] = OHLCV(
                    exchange=self.exchange, market_type=self.market_type, symbol=symbol,
                    interval=interval, ts=ts,
                    open=self._dec(row["open"]), high=self._dec(row["high"]),
                    low=self._dec(row["low"]), close=self._dec(row["close"]),
                    volume=self._dec(row.get("volume") or 0),
                )
            start = end
        return [bars[ts] for ts in sorted(bars)]

    async def fetch_funding(
        self, symbol: str, since: datetime
    ) -> Sequence[FundingRate]:
        """Settled hourly funding. Phoenix quotes an 8-hour rate and pays an
        eighth of it each hour; the history lists each payment as a percentage,
        stamped with the hour it was paid, stored as the fraction it charged."""
        now = datetime.now(timezone.utc)
        payload = await self._get(
            f"/v1/funding/{self.to_native(symbol)}/rates",
            startTime=_ms(max(since, now - timedelta(days=365))), endTime=_ms(now),
            limit="10000",
        )
        return [
            FundingRate(
                exchange=self.exchange, symbol=symbol,
                ts=datetime.fromtimestamp(int(row["timestamp"]), tz=timezone.utc),
                rate=self._dec(row["fundingRatePercentage"]) / 100, interval_hours=1,
            )
            for row in payload.get("rates") or []
        ]

    async def fetch_liquidity(
        self, symbols: Sequence[str]
    ) -> Sequence[LiquiditySnapshot]:
        if not symbols:
            return []
        stats = await self._market_stats(symbols)
        if not stats:
            raise RuntimeError("phoenix sent no market stats")
        now = datetime.now(timezone.utc)
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        out: list[LiquiditySnapshot] = []
        for symbol in symbols:
            row = stats.get(self.to_native(symbol))
            if row is None:
                continue
            out.append(LiquiditySnapshot(
                exchange=self.exchange, symbol=symbol, ts=now,
                open_interest=self._dec(row["openInterest"]),
                volume_24h=self._dec(row["dayNtlVlm"]),
                mark_price=self._dec(row["markPx"]),
                index_price=self._dec(row["oraclePx"]),
                # A percentage for the coming hourly payment, an eighth of the
                # 8-hour rate (live data; the documentation's example is a fraction).
                current_funding_rate=self._dec(row["funding"]) / 100,
                funding_interval_hours=1,
                next_funding_at=next_hour,
            ))
        return out

    async def fetch_bbo(self, symbol: str) -> TopOfBook:
        book = await self._get(f"/v1/view/orderbook/{self.to_native(symbol)}")
        bids = [(self._dec(p), self._dec(q)) for p, q in book.get("bids") or [] if q > 0]
        asks = [(self._dec(p), self._dec(q)) for p, q in book.get("asks") or [] if q > 0]
        if not bids or not asks:
            raise RuntimeError(f"phoenix returned an empty book for {symbol}")
        (bid_price, bid_size), (ask_price, ask_size) = max(bids), min(asks)
        return TopOfBook(
            exchange=self.exchange, symbol=symbol, quote_currency=self.quote_currency,
            # The book carries a Solana slot, not a time.
            ts=datetime.now(timezone.utc),
            bid_price=bid_price, bid_size=bid_size, ask_price=ask_price, ask_size=ask_size,
        )

    async def fetch_venue_volume(self) -> dict:
        """24h notional volume across every active Phoenix market."""
        markets = await self._get("/v1/view/exchange/markets")
        stats = await self._market_stats(
            [m["symbol"] for m in markets if m.get("marketStatus") == "active"]
        )
        if not stats:
            raise RuntimeError("phoenix sent no market stats")
        volume = sum((self._dec(row["dayNtlVlm"]) for row in stats.values()), Decimal(0))
        return {"spot": None, "perp": volume}
