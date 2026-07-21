from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, ClassVar

from core.enums import Exchange, MarketType, Timeframe
from core.models import OHLCV, FundingRate, LiquiditySnapshot
from data_collection.base import BaseExchangeScraper, Capability
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter

_INTERVALS: dict[Timeframe, str] = {
    Timeframe.M1: "PT1M", Timeframe.M5: "PT5M", Timeframe.M15: "PT15M",
    Timeframe.M30: "PT30M", Timeframe.H1: "PT1H", Timeframe.H2: "PT2H",
    Timeframe.H4: "PT4H", Timeframe.D1: "P1D",
}


def _from_ts_flexible(ts: int | float) -> datetime:
    """Docs declare ms but at least one example shows seconds — parse both."""
    if ts > 1e11:
        ts = ts / 1000
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _unwrap(payload: Any) -> Any:
    status = str(payload.get("status", "")).upper()
    if status not in ("OK",):
        err = payload.get("error") or {}
        raise RuntimeError(f"extended error {err.get('code')}: {err.get('message')}")
    return payload["data"]


class ExtendedScraper(BaseExchangeScraper):
    exchange: ClassVar[Exchange] = Exchange.EXTENDED
    base_url: ClassVar[str] = "https://api.starknet.extended.exchange"
    market_type: ClassVar[MarketType] = MarketType.PERP        # default/primary
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.OHLCV, Capability.FUNDING, Capability.LIQUIDITY, Capability.VENUE_VOLUME}
    )

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_minute(600, burst=60),     # docs: 1000/min default
            default_headers={"User-Agent": "overseer/0.1"},
        )

    # -- symbols: identity; type derivable from the naming convention ---------------

    def to_symbol(self, native: str) -> str:
        return native

    def to_native(self, symbol: str) -> str:
        return symbol

    @classmethod
    def market_type_for(cls, symbol: str) -> MarketType:
        # perps are "BTC-USD"; spot markets are "BTCSPOT"
        return MarketType.SPOT if symbol.upper().endswith("SPOT") else MarketType.PERP

    # -- OHLCV (no startTime param: filter client-side for resume) ------------------

    async def fetch_ohlcv(
        self, symbol: str, interval: Timeframe, since: datetime, *, limit: int = 1000
    ) -> Sequence[OHLCV]:
        payload = await self.http.get_json(
            f"{self.base_url}/api/v1/info/candles/{symbol}/trades",
            params={
                "interval": _INTERVALS[interval],
                "limit": str(limit),
                "endTime": str(self._to_ms(datetime.now(timezone.utc))),
            },
        )
        rows = _unwrap(payload)
        mt = self.market_type_for(symbol)
        since_ms = self._to_ms(since)
        out: list[OHLCV] = []
        for c in rows:
            ts = _from_ts_flexible(int(c["T"]))
            if self._to_ms(ts) < since_ms:
                continue
            out.append(
                OHLCV(
                    exchange=self.exchange,
                    market_type=mt,
                    symbol=symbol,
                    interval=interval,
                    ts=ts,
                    open=self._dec(c["o"]), high=self._dec(c["h"]),
                    low=self._dec(c["l"]), close=self._dec(c["c"]),
                    volume=self._dec(c.get("v", 0)),
                )
            )
        out.sort(key=lambda b: b.ts)          # API is newest-first
        return out

    # -- funding: applied hourly ------------------------------------------------------

    async def fetch_funding(
        self, symbol: str, since: datetime, *, limit: int = 1000
    ) -> Sequence[FundingRate]:
        payload = await self.http.get_json(
            f"{self.base_url}/api/v1/info/{symbol}/funding",
            params={
                "startTime": str(self._to_ms(since)),
                "endTime": str(self._to_ms(datetime.now(timezone.utc))),
                "limit": str(limit),
            },
        )
        rows = _unwrap(payload)
        out = [
            FundingRate(
                exchange=self.exchange,
                symbol=r.get("m", symbol),
                ts=_from_ts_flexible(int(r["T"])),
                rate=self._dec(r["f"]),
                interval_hours=1,
            )
            for r in rows
        ]
        out.sort(key=lambda r: r.ts)
        return out

    # -- liquidity: the markets call carries everything --------------------------------

    async def fetch_liquidity(
        self, symbols: Sequence[str]
    ) -> Sequence[LiquiditySnapshot]:
        params = "&".join(f"market={s}" for s in symbols)
        payload = await self.http.get_json(
            f"{self.base_url}/api/v1/info/markets?{params}"
        )
        rows = _unwrap(payload)
        wanted = set(symbols)
        now = datetime.now(timezone.utc)
        out: list[LiquiditySnapshot] = []
        for m in rows:
            if m["name"] not in wanted or m.get("type") != "PERPETUAL":
                continue
            stats = m.get("marketStats", {})
            out.append(
                LiquiditySnapshot(
                    exchange=self.exchange,
                    symbol=m["name"],
                    ts=now,
                    open_interest=self._dec(stats.get("openInterestBase", 0)),
                    volume_24h=self._dec(stats.get("dailyVolume", 0)),
                    mark_price=self._dec(stats.get("markPrice", 0)),
                )
            )
        return out

    # -- venue volume: same markets call, no market filter, ALL types summed -----

    async def fetch_venue_volume(self) -> dict:
        payload = await self.http.get_json(f"{self.base_url}/api/v1/info/markets")
        rows = _unwrap(payload)
        perp = spot = 0
        for m in rows:
            v = self._dec(m.get("marketStats", {}).get("dailyVolume", 0))
            if m.get("type") == "PERPETUAL":
                perp += v
            else:
                spot += v
        return {"spot": spot or None, "perp": perp or None}