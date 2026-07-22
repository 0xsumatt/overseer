from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, ClassVar

from core.enums import Exchange, MarketType, Side, Timeframe
from core.models import OHLCV, FundingRate, LiquiditySnapshot, Trade
from data_collection.base import BaseExchangeScraper, Capability
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter

_RESOLUTIONS: dict[Timeframe, str] = {
    Timeframe.M1: "1m", Timeframe.M5: "5m", Timeframe.M15: "15m",
    Timeframe.M30: "30m", Timeframe.H1: "1h", Timeframe.H4: "4h",
    Timeframe.H12: "12h", Timeframe.D1: "1d",
}


def _from_ts_flexible(ts: int | float) -> datetime:
    """Lighter mixes seconds and milliseconds across endpoints."""
    if ts > 1e11:                      # past ~5138 AD as seconds -> must be ms
        ts = ts / 1000
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class LighterScraper(BaseExchangeScraper):
    exchange: ClassVar[Exchange] = Exchange.LIGHTER
    base_url: ClassVar[str] = "https://mainnet.zklighter.elliot.ai"
    market_type: ClassVar[MarketType] = MarketType.PERP        # default/primary
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.OHLCV, Capability.FUNDING, Capability.LIQUIDITY, Capability.FILLS,
         Capability.VENUE_VOLUME}
    )

    def __init__(self, http: HttpClient | None = None) -> None:
        super().__init__(http)
        # symbol -> (market_id, market_type, stats-dict); lazily filled
        self._markets: dict[str, tuple[int, MarketType, dict]] | None = None

    def _build_http(self) -> HttpClient:
        return HttpClient(
            limiter=RateLimiter.per_minute(300, burst=20),     # conservative
            default_headers={"User-Agent": "overseer/0.1"},
        )

    # -- symbols: identity, HL-style convention ------------------------------------

    def to_symbol(self, native: str) -> str:
        return native

    def to_native(self, symbol: str) -> str:
        return symbol

    @classmethod
    def market_type_for(cls, symbol: str) -> MarketType:
        return MarketType.SPOT if "/" in symbol else MarketType.PERP

    @classmethod
    def is_fill_ref(cls, ref: object) -> bool:
        # Lighter accounts are integer indices (e.g. LLP = 281474976710654)
        return isinstance(ref, str) and ref.isdigit()

    # -- the market-id map (cached; also the liquidity source) ---------------------

    async def _market_map(self, refresh: bool = False) -> dict[str, tuple[int, MarketType, dict]]:
        if self._markets is None or refresh:
            payload = await self.http.get_json(f"{self.base_url}/api/v1/orderBookDetails")
            mapping: dict[str, tuple[int, MarketType, dict]] = {}
            for m in payload.get("order_book_details", []) or []:
                mapping[m["symbol"]] = (int(m["market_id"]), MarketType.PERP, m)
            for m in payload.get("spot_order_book_details", []) or []:
                mapping[m["symbol"]] = (int(m["market_id"]), MarketType.SPOT, m)
            self._markets = mapping
        return self._markets

    async def _market_id(self, symbol: str) -> int:
        markets = await self._market_map()
        if symbol not in markets:
            markets = await self._market_map(refresh=True)     # maybe newly listed
        if symbol not in markets:
            raise KeyError(f"lighter has no market {symbol!r}")
        return markets[symbol][0]

    # -- OHLCV ----------------------------------------------------------------------

    async def fetch_ohlcv(
        self, symbol: str, interval: Timeframe, since: datetime, *, limit: int = 500
    ) -> Sequence[OHLCV]:
        market_id = await self._market_id(symbol)
        payload = await self.http.get_json(
            f"{self.base_url}/api/v1/candles",
            params={
                "market_id": str(market_id),
                "resolution": _RESOLUTIONS[interval],
                "start_timestamp": str(self._to_ms(since)),
                "end_timestamp": str(self._to_ms(datetime.now(timezone.utc))),
                "count_back": str(limit),
            },
        )
        mt = self.market_type_for(symbol)
        out: list[OHLCV] = []
        for c in payload.get("c", []) or []:
            # zero-valued fields are omitted from the response
            out.append(
                OHLCV(
                    exchange=self.exchange,
                    market_type=mt,
                    symbol=symbol,
                    interval=interval,
                    ts=_from_ts_flexible(c["t"]),
                    open=self._dec(c.get("o", 0)),
                    high=self._dec(c.get("h", 0)),
                    low=self._dec(c.get("l", 0)),
                    close=self._dec(c.get("c", 0)),
                    volume=self._dec(c.get("v", 0)),           # base volume
                )
            )
        out.sort(key=lambda b: b.ts)
        return out

    # -- funding: hourly, unsigned rate + direction ---------------------------------

    async def fetch_funding(
        self, symbol: str, since: datetime, *, limit: int = 750
    ) -> Sequence[FundingRate]:
        market_id = await self._market_id(symbol)
        payload = await self.http.get_json(
            f"{self.base_url}/api/v1/fundings",
            params={
                "market_id": str(market_id),
                "resolution": "1h",
                "start_timestamp": str(self._to_ms(since)),
                "end_timestamp": str(self._to_ms(datetime.now(timezone.utc))),
                "count_back": str(limit),
            },
        )
        out: list[FundingRate] = []
        for f in payload.get("fundings", []) or []:
            # API's "rate" is a PERCENTAGE per hour (e.g. "0.0012" = 0.0012%),
            # not the fraction convention core.models.FundingRate documents
            # (0.0001 = 1bp = 0.01%) that every other venue's adapter already
            # follows — confirmed live 2026-07-22: raw "0.0012" annualizes to
            # a sane ~10.5% APR once divided by 100, vs an absurd ~1051% left
            # as-is (which is what was actually stored and tripping the
            # dislocation alert on every single Lighter reading).
            rate = self._dec(f["rate"]) / 100
            if f.get("direction") == "short":
                rate = -rate               # shorts pay -> negative funding
            out.append(
                FundingRate(
                    exchange=self.exchange,
                    symbol=symbol,
                    ts=_from_ts_flexible(int(f["timestamp"])),
                    rate=rate,
                    interval_hours=1,
                )
            )
        out.sort(key=lambda r: r.ts)
        return out

    # -- liquidity: the market map already holds it ----------------------------------

    async def fetch_liquidity(
        self, symbols: Sequence[str]
    ) -> Sequence[LiquiditySnapshot]:
        markets = await self._market_map(refresh=True)          # want fresh stats
        now = datetime.now(timezone.utc)
        out: list[LiquiditySnapshot] = []
        for symbol in symbols:
            entry = markets.get(symbol)
            if entry is None:
                continue
            _, _, stats = entry
            mark = stats.get("last_trade_price", 0)             # no mark px on REST
            out.append(
                LiquiditySnapshot(
                    exchange=self.exchange,
                    symbol=symbol,
                    ts=now,
                    open_interest=self._dec(stats.get("open_interest", 0)),
                    volume_24h=self._dec(stats.get("daily_quote_token_volume", 0)),
                    mark_price=self._dec(mark),
                )
            )
        return out

    # -- fills: GET /api/v1/trades for a tracked account (e.g. the LLP pool) ---------
    #    Public pools are queryable WITHOUT auth (the endpoint gates only master/
    #    sub accounts). DESC-only ordering, limit <= 100 per call: resume filters
    #    client-side on `since`, and like HLP-over-REST this is best-effort — a
    #    burst can outrun 100 fills/poll; the complete feed is the WS phase.

    async def fetch_fills(self, address: str, since: datetime) -> Sequence[Trade]:
        payload = await self.http.get_json(
            f"{self.base_url}/api/v1/trades",
            params={
                "account_index": address,
                "sort_by": "timestamp",       # required by the endpoint
                "sort_dir": "desc",
                "limit": "100",               # endpoint max
            },
        )
        markets = await self._market_map()
        by_id = {mid: (sym, mt) for sym, (mid, mt, _) in markets.items()}
        account = int(address)
        since_ms = self._to_ms(since)
        out: list[Trade] = []
        for t in payload.get("trades", []) or []:
            ts_raw = t.get("timestamp", 0)
            if ts_raw and ts_raw < since_ms and ts_raw > 1e11:
                continue                       # older than resume point (ms form)
            entry = by_id.get(int(t.get("market_id", -1)))
            if entry is None:
                continue                       # market not in our map (refresh next poll)
            symbol, mt = entry
            if int(t.get("bid_account_id", -1)) == account:
                side = Side.BUY
            elif int(t.get("ask_account_id", -1)) == account:
                side = Side.SELL
            else:
                continue                       # defensive: not our account's trade
            out.append(
                Trade(
                    exchange=self.exchange,
                    market_type=mt,
                    symbol=symbol,
                    trade_id=str(t.get("trade_id")),
                    price=self._dec(t["price"]),
                    amount=self._dec(t["size"]),
                    side=side,
                    ts=_from_ts_flexible(ts_raw),
                    wallet_address=address,    # account index, stored as string
                )
            )
        out.sort(key=lambda x: x.ts)
        return out

    # -- venue volume: same orderBookDetails call, summed over EVERY market ------

    async def fetch_venue_volume(self) -> dict:
        markets = await self._market_map(refresh=True)
        perp = spot = Decimal(0)
        for _, mt, stats in markets.values():
            v = self._dec(stats.get("daily_quote_token_volume", 0))
            if mt is MarketType.PERP:
                perp += v
            else:
                spot += v
        return {"spot": spot or None, "perp": perp or None}