from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import ClassVar

from core.enums import Exchange, MarketType, Side, Timeframe
from core.models import OHLCV, FundingRate, LiquiditySnapshot, Trade
from data_collection.base import BaseExchangeScraper, Capability
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter


class HyperliquidScraper(BaseExchangeScraper):
    exchange: ClassVar[Exchange] = Exchange.HYPERLIQUID
    base_url: ClassVar[str] = "https://api.hyperliquid.xyz"
    market_type: ClassVar[MarketType] = MarketType.PERP        # default/primary
    # FILLS is scheduled for specific known addresses (e.g. the HLP vault), not
    # as a market-wide tape — that's still WS's job. See scheduler/targets.py.
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.OHLCV, Capability.FILLS, Capability.FUNDING, Capability.LIQUIDITY}
    )

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_minute(1200, burst=60),    # conservative; tune
            default_headers={"User-Agent": "overseer/0.1"},
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

    @classmethod
    def market_type_for(cls, symbol: str) -> MarketType:
        return cls._market_for_coin(symbol)

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

    # -- fills: POST /info userFillsByTime — per-address utility, NOT scheduled ---
    #    Account-scoped, so it can't be the market-wide tape (that's WS later).
    #    Handy for "analyse this one wallet's fills/PnL" on demand.

    @classmethod
    def is_fill_ref(cls, ref: object) -> bool:
        return (
            isinstance(ref, str) and ref.startswith("0x") and len(ref) == 42
            and all(c in "0123456789abcdefABCDEF" for c in ref[2:])
        )

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

    # -- funding: POST /info fundingHistory (hourly settlements) ------------------

    async def fetch_funding(self, symbol: str, since: datetime) -> Sequence[FundingRate]:
        rows = await self.http.post_json(
            f"{self.base_url}/info",
            json={
                "type": "fundingHistory",
                "coin": self.to_native(symbol),
                "startTime": self._to_ms(since),
            },
        )
        return [
            FundingRate(
                exchange=self.exchange,
                symbol=self.to_symbol(r["coin"]),
                ts=self._from_ms(r["time"]),
                rate=self._dec(r["fundingRate"]),
                interval_hours=1,          # HL settles hourly
            )
            for r in rows
        ]

    # -- liquidity: POST /info metaAndAssetCtxs — ALL coins in one call -----------
    #    universe[i] (names) aligns index-wise with ctxs[i] (market data).

    async def fetch_liquidity(
        self, symbols: Sequence[str]
    ) -> Sequence[LiquiditySnapshot]:
        from datetime import datetime as _dt, timezone as _tz
        meta, ctxs = await self.http.post_json(
            f"{self.base_url}/info", json={"type": "metaAndAssetCtxs"}
        )
        wanted = {self.to_native(s) for s in symbols}
        now = _dt.now(_tz.utc)
        out: list[LiquiditySnapshot] = []
        for asset, ctx in zip(meta["universe"], ctxs):
            if asset["name"] not in wanted:
                continue
            mark = ctx.get("markPx") or ctx.get("oraclePx")
            out.append(
                LiquiditySnapshot(
                    exchange=self.exchange,
                    symbol=self.to_symbol(asset["name"]),
                    ts=now,
                    open_interest=self._dec(ctx["openInterest"]),
                    volume_24h=self._dec(ctx["dayNtlVlm"]),
                    mark_price=self._dec(mark),
                )
            )
        return out