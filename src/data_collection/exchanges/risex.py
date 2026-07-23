from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, ClassVar

from core.enums import Exchange, MarketType, Timeframe
from core.models import OHLCV, FundingRate, LiquiditySnapshot
from data_collection.base import BaseExchangeScraper, Capability
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter

_NS = 1_000_000_000

# Timeframe -> candle interval as a ns duration
_INTERVALS_NS: dict[Timeframe, int] = {
    Timeframe.M1: 60 * _NS, Timeframe.M5: 300 * _NS, Timeframe.M15: 900 * _NS,
    Timeframe.M30: 1800 * _NS, Timeframe.H1: 3600 * _NS, Timeframe.H4: 4 * 3600 * _NS,
    Timeframe.H12: 12 * 3600 * _NS, Timeframe.D1: 86400 * _NS,
}


def _to_ns(dt: datetime) -> int:
    return int(dt.timestamp() * _NS)


def _from_ns(ns: int | str) -> datetime:
    return datetime.fromtimestamp(int(ns) / _NS, tz=timezone.utc)


class RiseScraper(BaseExchangeScraper):
    exchange: ClassVar[Exchange] = Exchange.RISE
    base_url: ClassVar[str] = "https://api.rise.trade"
    market_type: ClassVar[MarketType] = MarketType.PERP       # perp-only venue
    supports_wide_liquidity: ClassVar[bool] = True
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.OHLCV, Capability.FUNDING, Capability.LIQUIDITY,
         Capability.VENUE_VOLUME}
    )

    _MARKET_MAP_TTL: ClassVar[float] = 1800.0

    def __init__(self, http: HttpClient | None = None) -> None:
        super().__init__(http)
        self._markets: dict[str, dict] | None = None      # name -> market info
        self._markets_at: float = 0.0

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_minute(300, burst=20),   # limits unpublished
            default_headers={"User-Agent": "overseer/0.1"},
        )

    # -- symbols: market names ("BTC/USDC"), identity mapping ----------------------
    # API is under active development (endpoints deprecated/changed without
    # notice) — re-verify this convention if fetches start failing.

    def to_symbol(self, native: str) -> str:
        return native

    def to_native(self, symbol: str) -> str:
        return symbol

    # -- market map (also the liquidity + venue-volume source) ---------------------

    async def _market_map(self, refresh: bool = False) -> dict[str, dict]:
        import time
        expired = (time.monotonic() - self._markets_at) > self._MARKET_MAP_TTL
        if self._markets is None or refresh or expired:
            params = {"force_refresh": "true"} if refresh else None
            payload = await self.http.get_json(f"{self.base_url}/v1/markets", params=params)
            markets = (payload.get("data") or {}).get("markets", [])
            mapping: dict[str, dict] = {}
            for m in markets or []:
                name = (m.get("config") or {}).get("name")
                if name:
                    mapping[name] = m
            self._markets = mapping
            self._markets_at = time.monotonic()
        return self._markets

    async def _market_id(self, symbol: str) -> int:
        markets = await self._market_map()
        if symbol not in markets:
            markets = await self._market_map(refresh=True)
        if symbol not in markets:
            raise KeyError(f"rise has no market {symbol!r}")
        return int(markets[symbol]["market_id"])

    def _is_live(self, m: dict) -> bool:
        return bool(m.get("active", True)) and bool((m.get("config") or {}).get("unlocked", True))

    # -- OHLCV: ns interval + ns window ---------------------------------------------

    async def fetch_ohlcv(
        self, symbol: str, interval: Timeframe, since: datetime, *, limit: int = 1000
    ) -> Sequence[OHLCV]:
        market_id = await self._market_id(symbol)
        payload = await self.http.get_json(
            f"{self.base_url}/v1/markets/id/{market_id}/trading-view-data",
            params={
                "interval": str(_INTERVALS_NS[interval]),
                "from": str(_to_ns(since)),
                "to": str(_to_ns(datetime.now(timezone.utc))),
            },
        )
        # trading-view-data nests candles under data.data (a second "data" key,
        # not a duplicate line) — see module docstring.
        candles = (payload.get("data") or {}).get("data", [])
        out = [
            OHLCV(
                exchange=self.exchange,
                market_type=self.market_type,
                symbol=symbol,
                interval=interval,
                ts=_from_ns(c["time"]),
                open=self._dec(c.get("open", 0) or 0),
                high=self._dec(c.get("high", 0) or 0),
                low=self._dec(c.get("low", 0) or 0),
                close=self._dec(c.get("close", 0) or 0),
                volume=self._dec(c.get("volume", 0) or 0),
            )
            for c in candles or []
        ]
        out.sort(key=lambda b: b.ts)
        return out

    # -- funding: settled events, interval derived PER RECORD -------------------------

    async def fetch_funding(
        self, symbol: str, since: datetime, *, limit: int = 1000, max_pages: int = 10
    ) -> Sequence[FundingRate]:
        market_id = await self._market_id(symbol)
        out: list[FundingRate] = []
        page = 1
        while page <= max_pages:
            payload = await self.http.get_json(
                f"{self.base_url}/v1/markets/id/{market_id}/funding-rate-history",
                params={
                    "start_time": str(_to_ns(since)),
                    "end_time": str(_to_ns(datetime.now(timezone.utc))),
                    "limit": str(limit),
                    "page": str(page),
                },
            )
            data = payload.get("data") or {}
            for r in data.get("records", []) or []:
                start_ns, end_ns = int(r["start_time"]), int(r["end_time"])
                hours = max(1, round((end_ns - start_ns) / (3600 * _NS)))
                out.append(
                    FundingRate(
                        exchange=self.exchange,
                        symbol=symbol,
                        ts=_from_ns(end_ns),           # settlement time keys the row
                        rate=self._dec(r["funding_rate"]),
                        interval_hours=hours,
                    )
                )
            if not data.get("has_next_page"):
                break
            page += 1
        out.sort(key=lambda x: x.ts)
        return out

    # -- liquidity: the market map already carries it ---------------------------------

    async def fetch_liquidity(
        self, symbols: Sequence[str]
    ) -> Sequence[LiquiditySnapshot]:
        markets = await self._market_map(refresh=True)
        now = datetime.now(timezone.utc)
        wanted = list(symbols) if symbols else [
            name for name, m in markets.items() if self._is_live(m)
        ]
        out: list[LiquiditySnapshot] = []
        for symbol in wanted:
            m = markets.get(symbol)
            if m is None:
                continue
            out.append(
                LiquiditySnapshot(
                    exchange=self.exchange,
                    symbol=symbol,
                    ts=now,
                    open_interest=self._dec(m.get("open_interest", 0) or 0),
                    volume_24h=self._dec(m.get("quote_volume_24h", 0) or 0),
                    mark_price=self._dec(m.get("mark_price", 0) or 0),
                )
            )
        return out

    # -- discovery + venue volume ------------------------------------------------------

    async def list_perp_symbols(self) -> list[str]:
        markets = await self._market_map(refresh=True)
        return [name for name, m in markets.items() if self._is_live(m)]

    async def fetch_venue_volume(self) -> dict:
        markets = await self._market_map(refresh=True)
        total = sum(
            Decimal(str(m.get("quote_volume_24h", 0) or 0)) for m in markets.values()
        )
        return {"spot": None, "perp": total}