from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import ClassVar

from core.enums import Exchange, MarketType, QuoteCurrency, Side, Timeframe
from core.models import FundingRate, LiquiditySnapshot, OHLCV, TopOfBook, Trade
from data_collection.base import BaseExchangeScraper, Capability
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter


class HyperliquidScraper(BaseExchangeScraper):
    exchange: ClassVar[Exchange] = Exchange.HYPERLIQUID
    base_url: ClassVar[str] = "https://api.hyperliquid.xyz"
    market_type: ClassVar[MarketType] = MarketType.PERP  
    quote_currency: ClassVar[QuoteCurrency] = QuoteCurrency.USDC
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {
            Capability.OHLCV,
            Capability.FILLS,
            Capability.FUNDING,
            Capability.LIQUIDITY,
            Capability.VENUE_VOLUME,
            Capability.BBO,
        }
    )

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_minute(1200, burst=60), 
            default_headers={"User-Agent": "overseer/0.1"},
        )

    @staticmethod
    def _dex_for_symbol(symbol: str) -> str:
        return symbol.split(":", 1)[0] if ":" in symbol else ""

    async def _asset_contexts(self, dex: str = "") -> tuple[dict, list[dict]]:
        request = {"type": "metaAndAssetCtxs"}
        if dex:
            request["dex"] = dex
        meta, contexts = await self.http.post_json(
            f"{self.base_url}/info", json=request
        )
        return meta, contexts

    @staticmethod
    def _market_for_coin(coin: str) -> MarketType:
        if "/" in coin or coin.startswith("@"):
            return MarketType.SPOT
        return MarketType.PERP

   
    def to_symbol(self, native: str) -> str:
        return native

    def to_native(self, symbol: str) -> str:
        return symbol

    @classmethod
    def market_type_for(cls, symbol: str) -> MarketType:
        return cls._market_for_coin(symbol)

    async def fetch_bbo(self, symbol: str) -> TopOfBook:
        coin = self.to_native(symbol)
        payload = await self.http.post_json(
            f"{self.base_url}/info", json={"type": "l2Book", "coin": coin}
        )
        bids, asks = payload.get("levels", ([], []))
        if not bids or not asks:
            raise RuntimeError(f"hyperliquid returned an empty book for {coin}")
        bid, ask = bids[0], asks[0]
        return TopOfBook(
            exchange=self.exchange,
            symbol=self.to_symbol(coin),
            quote_currency=self.quote_currency,
            ts=datetime.now(timezone.utc),
            bid_price=self._dec(bid["px"]),
            bid_size=self._dec(bid["sz"]),
            ask_price=self._dec(ask["px"]),
            ask_size=self._dec(ask["sz"]),
        )


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
                interval_hours=1,         
            )
            for r in rows
        ]


    async def fetch_liquidity(
        self, symbols: Sequence[str]
    ) -> Sequence[LiquiditySnapshot]:
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        symbols_by_dex: dict[str, set[str]] = {}
        for symbol in symbols:
            native = self.to_native(symbol)
            symbols_by_dex.setdefault(self._dex_for_symbol(native), set()).add(native)

        now = _dt.now(_tz.utc)
        next_funding = now.replace(minute=0, second=0, microsecond=0) + _td(hours=1)
        out: list[LiquiditySnapshot] = []
        for dex, wanted in symbols_by_dex.items():
            meta, contexts = await self._asset_contexts(dex)
            for asset, ctx in zip(meta["universe"], contexts):
                if asset["name"] not in wanted:
                    continue
                mark = ctx.get("markPx") or ctx.get("oraclePx")
                current_rate = ctx.get("funding")
                premium = ctx.get("premium")
                out.append(
                    LiquiditySnapshot(
                        exchange=self.exchange,
                        symbol=self.to_symbol(asset["name"]),
                        ts=now,
                        open_interest=self._dec(ctx["openInterest"]),
                        volume_24h=self._dec(ctx["dayNtlVlm"]),
                        mark_price=self._dec(mark),
                        index_price=self._dec(ctx["oraclePx"]),
                        current_funding_rate=(
                            self._dec(current_rate)
                            if current_rate not in (None, "")
                            else None
                        ),
                        funding_interval_hours=1,
                        next_funding_at=next_funding,
                        funding_premium=(
                            self._dec(premium)
                            if premium not in (None, "")
                            else None
                        ),
                    )
                )
        return out


    async def fetch_venue_volume(self) -> dict:
        _, contexts = await self._asset_contexts()
        perp = sum((self._dec(c["dayNtlVlm"]) for c in contexts), self._dec(0))

        dexes = await self.http.post_json(
            f"{self.base_url}/info", json={"type": "perpDexs"}
        )
        for dex in dexes:
            if not isinstance(dex, dict) or not dex.get("name"):
                continue
            _, dex_contexts = await self._asset_contexts(dex["name"])
            perp += sum(
                (self._dec(c["dayNtlVlm"]) for c in dex_contexts),
                self._dec(0),
            )

        _, spot_contexts = await self.http.post_json(
            f"{self.base_url}/info", json={"type": "spotMetaAndAssetCtxs"}
        )
        spot = sum(
            (self._dec(c["dayNtlVlm"]) for c in spot_contexts),
            self._dec(0),
        )
        return {"spot": spot or None, "perp": perp or None}

class HyperliquidDeployerScraper(HyperliquidScraper):
    """HIP-3 market adapter; deployer volume is included by the parent venue."""

    capabilities: ClassVar[frozenset[Capability]] = (
        HyperliquidScraper.capabilities
        - frozenset({Capability.FILLS, Capability.VENUE_VOLUME})
    )