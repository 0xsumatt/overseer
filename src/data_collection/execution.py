"""Transient public perpetual depth, separate from collector/BBO contracts.

Prices and quantities retain the public REST human units. Native price accounting
is not inferred from margin tokens. No snapshot is cached or extrapolated.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
import time
from urllib.parse import quote

from core.enums import MarketType
from core.execution import BookLevel, DepthBook
from data_collection.exchanges.binance import BinanceFuturesScraper
from data_collection.exchanges.bulk import BulkScraper
from data_collection.exchanges.bullet import BulletScraper
from data_collection.exchanges.bybit import BybitPerpScraper
from data_collection.exchanges.extended import ExtendedScraper, _unwrap
from data_collection.exchanges.hyperliquid import HyperliquidScraper
from data_collection.exchanges.lighter import LighterScraper
from data_collection.exchanges.risex import RiseScraper
from data_collection.http import HttpClient


class DepthUnavailable(ValueError):
    """An intentionally safe public diagnostic, never an upstream response."""


def number(value) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise DepthUnavailable("Invalid financial number in public depth.")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise DepthUnavailable("Invalid financial number in public depth.") from None
    if not result.is_finite() or result <= 0 or abs(result.adjusted()) > 60 or len(result.as_tuple().digits) > 80:
        raise DepthUnavailable("Invalid financial number in public depth.")
    return result


def optional_limit(value) -> Decimal | None:
    # Explicit zero means the public filter is disabled, not a zero-size order.
    if value is None or value == "":
        return None
    if isinstance(value, (str, int, Decimal)) and not isinstance(value, bool):
        try:
            if Decimal(value) == 0:
                return None
        except InvalidOperation:
            pass
    return number(value)


def levels(rows, price_key, quantity_key, *, bids: bool) -> tuple[BookLevel, ...]:
    if not isinstance(rows, list):
        raise DepthUnavailable("Invalid public depth structure.")
    combined = {}
    with localcontext() as context:
        context.prec = 100
        for row in rows:
            price, quantity = number(row[price_key]), number(row[quantity_key])
            combined[price] = combined.get(price, Decimal(0)) + quantity
    return tuple(BookLevel(price, combined[price]) for price in sorted(combined, reverse=bids))


def source_time(value, unit: str) -> datetime | None:
    if value is None:
        return None
    stamp = number(value)
    if stamp != stamp.to_integral_value():
        raise DepthUnavailable("Invalid public snapshot timestamp.")
    divisor = {"ms": 1000, "ns": 1_000_000_000}[unit]
    seconds, remainder = divmod(int(stamp), divisor)
    try:
        result = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            seconds=seconds, microseconds=remainder * 1_000_000 // divisor)
    except (OverflowError, ValueError):
        raise DepthUnavailable("Invalid public snapshot timestamp.") from None
    if not 2000 <= result.year <= 2100:
        raise DepthUnavailable("Unrecognized public snapshot timestamp units.")
    return result


def currency(value) -> str:
    if value not in ("USD", "USDC", "USDT"):
        raise DepthUnavailable("Unsupported or missing native price denomination.")
    return value


def identity(actual, expected) -> None:
    if actual != expected:
        raise DepthUnavailable("Public market identity does not match the requested listing.")


def one_market(rows, key, symbol):
    matches = [row for row in rows if row.get(key) == symbol]
    if len(matches) != 1:
        raise DepthUnavailable("Public market metadata is missing or ambiguous.")
    return matches[0]


def _reject_constant(_value):
    raise ValueError("Nonfinite JSON number")


class DecimalHttpClient(HttpClient):
    """Use raw decimal tokens, including Bulk's numeric REST price/size fields."""

    async def _json(self, method, url, **kwargs):
        response = await self.request(method, url, retry=False, **kwargs)
        return json.loads(await response.text(), parse_float=Decimal,
                          parse_constant=_reject_constant)

    async def get_json(self, url, **kwargs):
        return await self._json("get", url, **kwargs)

    async def post_json(self, url, *, retry=False, **kwargs):
        return await self._json("post", url, **kwargs)


class ExecutionDepth:
    """One loop-owned provider; caches only public metadata (at most 60s)."""

    metadata_ttl = 60

    def __init__(self, clients: dict[str, HttpClient]):
        self.clients = clients
        self.binance = BinanceFuturesScraper(clients["binance_perp"])
        self.bybit = BybitPerpScraper(clients["bybit_perp"])
        self.lighter = LighterScraper(clients["lighter"])
        self.rise = RiseScraper(clients["rise"])
        self._metadata = {}
        self._locks = {}

    async def _cached(self, key, load):
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._metadata.get(key)
            if cached is not None and time.monotonic() - cached[0] < self.metadata_ttl:
                return cached[1]
            value = await load()
            self._metadata[key] = (time.monotonic(), value)
            return value

    async def fetch(self, venue: str, symbol: str) -> DepthBook:
        if venue in ("hyperliquid", "hyperliquid_xyz", "hyperliquid_io"):
            return await self._hyperliquid(venue, symbol)
        handler = {
            "binance_perp": self._binance, "bybit_perp": self._bybit,
            "lighter": self._lighter, "extended": self._extended,
            "rise": self._rise, "bullet": self._bullet, "bulk": self._bulk,
        }.get(venue)
        if handler is None:
            raise DepthUnavailable("Public perpetual depth is unsupported for this venue.")
        return await handler(venue, symbol)

    @staticmethod
    def _book(venue, symbol, quote_currency, payload, bid_key, ask_key,
              price_key, quantity_key, *, venue_at=None, notes=(),
              max_quantity=None, max_quote_notional=None):
        received = datetime.now(timezone.utc)
        bids = levels(payload[bid_key], price_key, quantity_key, bids=True)
        asks = levels(payload[ask_key], price_key, quantity_key, bids=False)
        if not bids or not asks:
            raise DepthUnavailable("Public depth has an empty side.")
        if bids[0].price > asks[0].price:
            raise DepthUnavailable("Public depth is crossed.")
        return DepthBook(venue=venue, symbol=symbol, quote_currency=currency(quote_currency),
                         captured_at=received, bids=bids, asks=asks, venue_at=venue_at,
                         max_quantity=max_quantity, max_quote_notional=max_quote_notional,
                         notes=tuple(notes))

    async def _binance(self, venue, symbol):
        http = self.clients[venue]
        native = self.binance.to_native(symbol)
        async def load():
            return await http.get_json(f"{self.binance.base_url}/fapi/v1/exchangeInfo")
        market = one_market((await self._cached(venue, load))["symbols"], "symbol", native)
        if market.get("status") != "TRADING" or market.get("contractType") != "PERPETUAL":
            raise DepthUnavailable("Market is not an active perpetual.")
        quote_currency = currency(market["quoteAsset"])
        identity(native, market["baseAsset"] + quote_currency)
        limits = [row for row in market.get("filters", []) if row.get("filterType") == "MARKET_LOT_SIZE"]
        max_quantity = optional_limit(limits[0].get("maxQty")) if len(limits) == 1 else None
        payload = await http.get_json(f"{self.binance.base_url}/fapi/v1/depth",
                                      params={"symbol": native, "limit": "1000"}, weight=20)
        if "symbol" in payload:
            identity(payload["symbol"], native)
        return self._book(venue, symbol, quote_currency, payload, "bids", "asks", 0, 1,
                          venue_at=source_time(payload.get("T", payload.get("E")), "ms"),
                          max_quantity=max_quantity,
                          notes=("Up to 1000 visible price levels per side; request weight 20. RPI orders excluded.",))

    async def _bybit(self, venue, symbol):
        http = self.clients[venue]
        native = self.bybit.to_native(symbol)
        async def load():
            payload = await http.get_json(f"{self.bybit.base_url}/v5/market/instruments-info",
                                          params={"category": "linear", "symbol": native})
            return self.bybit._unwrap(payload)["list"]
        market = one_market(await self._cached((venue, native), load), "symbol", native)
        if market.get("status") != "Trading" or market.get("contractType") != "LinearPerpetual":
            raise DepthUnavailable("Market is not an active linear perpetual.")
        quote_currency = currency(market["quoteCoin"])
        identity(native, market["baseCoin"] + quote_currency)
        payload = self.bybit._unwrap(await http.get_json(f"{self.bybit.base_url}/v5/market/orderbook",
                                      params={"category": "linear", "symbol": native, "limit": "1000"}))
        identity(payload["s"], native)
        return self._book(venue, symbol, quote_currency, payload, "b", "a", 0, 1,
                          venue_at=source_time(payload.get("cts", payload.get("ts")), "ms"),
                          max_quantity=optional_limit(market.get("lotSizeFilter", {}).get("maxMktOrderQty")),
                          notes=("Up to 1000 visible price levels per side; RPI orders excluded.",))

    async def _hyperliquid(self, venue, symbol):
        http = self.clients["hyperliquid"]
        dex = HyperliquidScraper._dex_for_symbol(symbol)
        if (venue == "hyperliquid" and dex) or (venue != "hyperliquid" and dex != venue.removeprefix("hyperliquid_")):
            raise DepthUnavailable("Hyperliquid deployer identity does not match the requested listing.")
        async def load():
            params = {"type": "metaAndAssetCtxs"}
            if dex:
                params["dex"] = dex
            response = await http.post_json(f"{HyperliquidScraper.base_url}/info", json=params, weight=20)
            meta = response[0]
            if dex:
                spot = await self._cached("hl_spot", lambda: http.post_json(
                    f"{HyperliquidScraper.base_url}/info", json={"type": "spotMeta"}, weight=20))
                tokens = {int(row["index"]): row["name"] for row in spot["tokens"]}
                accounting = currency(tokens[int(meta["collateralToken"])])
            else:
                accounting = "USDC"
            return meta, accounting
        meta, accounting = await self._cached(("hl", dex), load)
        market = one_market(meta["universe"], "name", symbol)
        if market.get("isDelisted", False):
            raise DepthUnavailable("Market is delisted.")
        notes = ["Full-precision snapshot; at most 20 visible price levels per side, not full depth."]
        max_notional = None
        if dex:
            notes.append(f"HIP-3 {dex} prices and order notionals use {accounting} accounting; collateral metadata is resolved separately from native oracle conventions.")
        else:
            oracle = "USDC" if symbol in ("HYPE", "PURR") else "USDT"
            notes.append(f"Native {oracle}-oracle contract, USDC price/PnL and order-notional accounting. Quanto settlement applies no USDC/USDT conversion; USD FX is reference valuation only.")
            leverage = number(market["maxLeverage"])
            max_notional = Decimal("30000000" if leverage >= 25 else "5000000" if leverage >= 20 else "2000000" if leverage >= 10 else "500000")
            notes.append("Native MARKET-order value cap follows contract specifications and the market's maximum leverage, not the chosen account leverage.")
        payload = await http.post_json(f"{HyperliquidScraper.base_url}/info",
                                      json={"type": "l2Book", "coin": symbol}, weight=2)
        identity(payload["coin"], symbol)
        sides = payload["levels"]
        if len(sides) != 2:
            raise DepthUnavailable("Invalid public depth structure.")
        return self._book(venue, symbol, accounting, {"bids": sides[0], "asks": sides[1]},
                          "bids", "asks", "px", "sz", venue_at=source_time(payload.get("time"), "ms"),
                          max_quote_notional=max_notional, notes=notes)

    async def _lighter(self, venue, symbol):
        async def load():
            return await self.lighter._market_map(refresh=True)
        markets = await self._cached(venue, load)
        if symbol not in markets:
            raise DepthUnavailable("Public perpetual market metadata is missing.")
        market_id, kind, market = markets[symbol]
        if (kind != MarketType.PERP or market.get("status") not in (None, "active", "ACTIVE")
                or market.get("is_frozen", False)
                or market.get("market_config", {}).get("force_reduce_only", False)):
            raise DepthUnavailable("Market is not an active perpetual.")
        identity(market["symbol"], symbol)
        payload = await self.clients[venue].get_json(f"{self.lighter.base_url}/api/v1/orderBookOrders",
                                             params={"market_id": str(market_id), "limit": "250"})
        if payload.get("code") != 200:
            raise DepthUnavailable("Public depth provider rejected the request.")
        if "market_id" in payload:
            identity(str(payload["market_id"]), str(market_id))
        for side in ("bids", "asks"):
            for row in payload[side]:
                if "market_id" in row:
                    identity(str(row["market_id"]), str(market_id))
        return self._book(venue, symbol, "USDC", payload, "bids", "asks", "price", "remaining_base_amount",
                          notes=("Up to 250 individual orders per side, combined at equal prices; remaining human base amounts, not initial amounts or integer transaction units.",
                                 "USDC perpetual price accounting; quote_asset_id is not a spot-token denomination. No source snapshot time; receipt time only.",
                                 "Reported order totals are not treated as proof of full-book coverage."))

    async def _extended(self, venue, symbol):
        http = self.clients[venue]
        async def load():
            return _unwrap(await http.get_json(f"{ExtendedScraper.base_url}/api/v1/info/markets", params={"market": symbol}))
        market = one_market(await self._cached((venue, symbol), load), "name", symbol)
        if market.get("isRfq") is not False:
            raise DepthUnavailable("RFQ indicative REST depth is not executable; public REST estimates are unsupported.")
        if market.get("active") is not True or market.get("status") != "ACTIVE" or market.get("isOffHours", False):
            raise DepthUnavailable("Market is suspended, inactive or outside trading hours.")
        if market.get("type") != "PERPETUAL":
            raise DepthUnavailable("Market is not a perpetual.")
        accounting = currency(market["collateralAssetName"])
        identity(symbol, market["assetName"] + "-" + accounting)
        payload = _unwrap(await http.get_json(f"{ExtendedScraper.base_url}/api/v1/info/markets/{quote(symbol, safe='')}/orderbook"))
        if "market" in payload:
            identity(payload["market"], symbol)
        return self._book(venue, symbol, accounting, payload, "bid", "ask", "price", "qty",
                          max_quote_notional=optional_limit(market.get("tradingConfig", {}).get("maxMarketOrderValue")),
                          notes=(f"Market metadata denotes prices/order values in {accounting}; this is not a collateral-token FX peg.",
                                 "REST publishes no supported depth limit or snapshot time. Captured visible levels only; receipt time is not matching-engine freshness."))

    async def _rise(self, venue, symbol):
        async def load():
            return await self.rise._market_map(refresh=True)
        markets = await self._cached(venue, load)
        market = markets.get(symbol)
        if market is None:
            raise DepthUnavailable("Public perpetual market metadata is missing.")
        identity(market["config"]["name"], symbol)
        if not self.rise._is_live(market) or market.get("post_only", False) or market.get("reduce_only", False):
            raise DepthUnavailable("Market is inactive or restricted to non-market orders.")
        accounting = currency(market["quote_asset_symbol"])
        if "/" in symbol:
            identity(symbol.rsplit("/", 1)[1], accounting)
        base = market["base_asset_symbol"]
        if base not in (symbol, symbol.split("/", 1)[0]):
            raise DepthUnavailable("Contradictory market base denomination.")
        market_id = str(market["market_id"])
        payload = await self.clients[venue].get_json(f"{self.rise.base_url}/v1/orderbook",
                                                    params={"market_id": market_id, "limit": "250"})
        book = payload["data"]
        identity(str(book["market_id"]), market_id)
        totals = []
        for side in ("bids", "asks"):
            total = book.get(f"total_{side}")
            if total is not None:
                parsed = int(total)
                if isinstance(total, bool) or str(parsed) != str(total) or parsed < len(book[side]):
                    raise DepthUnavailable("Contradictory full-book level count.")
                totals.append(f"{side}: {len(book[side])} captured of {parsed} reported price levels")
        return self._book(venue, symbol, accounting, book, "bids", "asks", "price", "quantity",
                          notes=("Up to 250 price levels per side; human quantities are not divided by 10^18. No source snapshot time; receipt time only.", *totals))

    async def _exchange_info(self, venue, base_url):
        async def load():
            return await self.clients[venue].get_json(f"{base_url}/exchangeInfo")
        info = await self._cached(venue, load)
        return info if isinstance(info, list) else info["symbols"]

    @staticmethod
    def _filter_limits(market):
        maximum = None
        for row in market.get("filters", []):
            if row.get("filterType") == "MARKET_LOT_SIZE":
                maximum = optional_limit(row.get("maxQty"))
        return maximum

    async def _bullet(self, venue, symbol):
        base_url = f"{BulletScraper.base_url}/fapi/v1"
        market = one_market(await self._exchange_info(venue, base_url), "symbol", symbol)
        if market.get("status") != "TRADING":
            raise DepthUnavailable("Market is suspended or inactive.")
        accounting = currency(market["quoteAsset"])
        # Bullet's symbol suffix may be USD while quoteAsset is USDT; the
        # authoritative quote field must not be replaced with marginAsset.
        identity(symbol.split("-", 1)[0], market["baseAsset"])
        payload = await self.clients[venue].get_json(f"{base_url}/depth", params={"symbol": symbol, "limit": "1000"})
        if "symbol" in payload:
            identity(payload["symbol"], symbol)
        raw_times = ", ".join(f"{key}={number(payload[key])}" for key in ("E", "T") if key in payload)
        return self._book(venue, symbol, accounting, payload, "bids", "asks", 0, 1,
                          max_quantity=self._filter_limits(market),
                          notes=(f"Current exchangeInfo quotes {accounting}, distinct from settlement/margin {market.get('marginAsset', 'unspecified')}.",
                                 f"Current live REST documentation allows up to 1000 levels per side; provider-polled snapshot. Raw {raw_times or 'E/T absent'}; source time units unverified, so receipt time only.",
                                 "[INFERENCE] Human base size follows the published Binance FAPI-compatible contract and fractional BTC sizes in the official SDK; the depth schema does not separately name quantity units."))

    async def _bulk(self, venue, symbol):
        market = one_market(await self._exchange_info(venue, BulkScraper.base_url), "symbol", symbol)
        if market.get("status") != "TRADING":
            raise DepthUnavailable("Market is suspended or inactive.")
        accounting = currency(market["quoteAsset"])
        # '-USD' is the market name, not a substitute for current quoteAsset.
        identity(symbol.split("-", 1)[0], market["baseAsset"])
        payload = await self.clients[venue].get_json(f"{BulkScraper.base_url}/l2book",
                              params={"type": "l2book", "coin": symbol, "nlevels": "1000"})
        identity(payload["symbol"], symbol)
        if payload.get("updateType") != "snapshot" or len(payload["levels"]) != 2:
            raise DepthUnavailable("Provider did not return a complete snapshot envelope.")
        stamp = payload.get("timestamp")
        # Live ns epochs contradict the docs' ms description. Recognize only
        # these evidenced two units, validate epoch range, preserve ns in notes.
        unit = "ns" if stamp is not None and number(stamp) >= Decimal("1000000000000000000") else "ms"
        return self._book(venue, symbol, accounting,
                          {"bids": payload["levels"][0], "asks": payload["levels"][1]},
                          "bids", "asks", "px", "sz", venue_at=source_time(stamp, unit),
                          max_quantity=self._filter_limits(market),
                          notes=(f"Current exchangeInfo price denomination is {accounting}, distinct from USDC settlement; no stablecoin peg.",
                                 "Requested 1000 price levels per side, not a documented server maximum. Human REST sizes are not multiplied/divided by 10^4.",
                                 (f"Source timestamp {stamp} interpreted as {unit}; observed ns epochs differ from the documentation's ms description."
                                  if stamp is not None else "No source snapshot timestamp; receipt time only.")))
