"""Hyperliquid adapter — a POST-only venue, and the on-chain/perps source.

Everything goes through one endpoint: ``POST /info`` with a JSON body whose
``type`` selects the query. It's a perps DEX, so the public trade tape — and,
because the book is on-chain, the per-trade counterparty addresses — comes from
the websocket layer later, not REST. The only *scheduled* REST feed here is
candles (``candleSnapshot``).

``fetch_fills`` (``userFillsByTime``) is kept as an on-demand, per-address
utility (e.g. analysing one wallet's PnL). It is account-scoped, so it can never
be the market-wide tape; it is deliberately NOT in ``capabilities`` and is never
scheduled as market-data ingest.

Coin format encodes the market: a bare name ("BTC") or dex-prefixed name
("xyz:XYZ100") is a perp; "X/USDC" or "@{index}" is spot. So market_type is
derived per record rather than fixed for the whole adapter.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import ClassVar

from core.enums import Exchange, MarketType, Side, Timeframe
from core.models import OHLCV, Trade
from data_collection.base import BaseExchangeScraper, Capability
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter


class HyperliquidScraper(BaseExchangeScraper):
    exchange: ClassVar[Exchange] = Exchange.Hyperliquid
    base_url: ClassVar[str] = "https://api.hyperliquid.xyz"
    market_type: ClassVar[MarketType] = MarketType.PERP        # default/primary
    # FILLS is scheduled for specific known addresses (e.g. the HLP vault), not
    # as a market-wide tape — that's still WS's job. See scheduler/targets.py.
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.OHLCV, Capability.FILLS}
    )

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_minute(1200, burst=60),    # conservative; tune
            default_headers={"User-Agent": "cryptodash/0.1"},
        )

    # -- symbols ------------------------------------------------------------------

    @staticmethod
    def _market_for_coin(coin: str) -> MarketType:
        # spot coins are "X/USDC" or "@{index}"; everything else is a perp
        if "/" in coin or coin.startswith("@"):
            return MarketType.SPOT
        return MarketType.PERP

    # Hyperliquid's own coin format already works as an unambiguous canonical
    # within the venue: "BTC" is a perp, "PURR/USDC" / "@1" are spot, "dex:NAME"
    # is a HIP-3 perp. So translation is identity, and market_type derives
    # straight from the symbol. (An earlier "BTC" -> "BTC/USDC" mapping collided
    # with spot "PURR/USDC" and made the market undecidable from the string.)
    # Reconciling HL "BTC" with Binance "BTC/USDT" is symbols.py's job, deferred.
    def to_symbol(self, native: str) -> str:
        return native

    def to_native(self, symbol: str) -> str:
        return symbol

    def market_type_for(self, symbol: str) -> MarketType:
        return self._market_for_coin(symbol)

    # -- OHLCV: POST /info candleSnapshot -----------------------------------------

    async def fetch_ohlcv(
        self, symbol: str, interval: Timeframe, since: datetime, *, until: datetime | None = None
    ) -> Sequence[OHLCV]:
        coin = self.to_native(symbol)
        end_ms = self._to_ms(until) if until is not None else self._to_ms(datetime.now().astimezone())
        candles = await self.http.post_json(
            f"{self.base_url}/info",
            json={
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": interval.value,
                    "startTime": self._to_ms(since),
                    "endTime": end_ms,
                },
            },
        )
        out: list[OHLCV] = []
        for c in candles:
            out.append(
                OHLCV(
                    exchange=self.exchange,
                    market_type=self._market_for_coin(c["s"]),
                    symbol=self.to_symbol(c["s"]),
                    interval=interval,
                    ts=self._from_ms(c["t"]),       # bar open time
                    open=self._dec(c["o"]),
                    high=self._dec(c["h"]),
                    low=self._dec(c["l"]),
                    close=self._dec(c["c"]),
                    volume=self._dec(c["v"]),
                )
            )
        return out

   

    async def fetch_fills(self, address: str, since: datetime) -> Sequence[Trade]:
        fills = await self.http.post_json(
            f"{self.base_url}/info",
            json={
                "type": "userFillsByTime",
                "user": address,
                "startTime": self._to_ms(since),
            },
        )
        out: list[Trade] = []
        for f in fills:
            out.append(
                Trade(
                    exchange=self.exchange,
                    market_type=self._market_for_coin(f["coin"]),
                    symbol=self.to_symbol(f["coin"]),
                    trade_id=str(f["tid"]),
                    price=self._dec(f["px"]),
                    amount=self._dec(f["sz"]),
                    side=Side.BUY if f["side"] == "B" else Side.SELL,
                    ts=self._from_ms(f["time"]),
                    wallet_address=address,         # the on-chain payoff
                )
            )
        return out