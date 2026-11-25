from __future__ import annotations

from typing import Any

import pytest

from core.models import Listing
from data_collection.base import Capability
from data_collection.exchanges.binance import BinanceFuturesScraper, BinanceSpotScraper
from data_collection.exchanges.bullet import BulletScraper
from data_collection.exchanges.bybit import BybitPerpScraper, BybitSpotScraper


class RoutingHTTP:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get_json(self, url: str, *, params: dict[str, str] | None = None, **_: Any) -> Any:
        self.calls.append((url, dict(params or {})))
        for marker, response in self.responses.items():
            if marker in url:
                return response(params or {}) if callable(response) else response
        raise AssertionError(f"unexpected request: {url}")


def _binance(symbol: str, base: str, quote: str, status: str, contract: str | None = None) -> dict:
    row = {"symbol": symbol, "baseAsset": base, "quoteAsset": quote, "status": status}
    if contract is not None:
        row["contractType"] = contract
    return row


@pytest.mark.asyncio
async def test_binance_spot_lists_open_and_pre_trading_usdt_pairs() -> None:
    http = RoutingHTTP({"/api/v3/exchangeInfo": {"symbols": [
        _binance("BTCUSDT", "BTC", "USDT", "TRADING"),
        _binance("NEWUSDT", "NEW", "USDT", "PRE_TRADING"),
        _binance("OLDUSDT", "OLD", "USDT", "BREAK"),        # delisted
        _binance("ETHBTC", "ETH", "BTC", "TRADING"),        # other quote
        _binance("ETHUSDC", "ETH", "USDC", "TRADING"),
    ]}})
    scraper = BinanceSpotScraper(http)
    assert Capability.MARKETS in scraper.capabilities
    assert await scraper.fetch_markets() == [
        Listing("BTC/USDT", "BTC"), Listing("NEW/USDT", "NEW"),
    ]


@pytest.mark.asyncio
async def test_binance_futures_lists_usdt_perps_only() -> None:
    http = RoutingHTTP({"/fapi/v1/exchangeInfo": {"symbols": [
        _binance("BTCUSDT", "BTC", "USDT", "TRADING", "PERPETUAL"),
        _binance("NVDAUSDT", "NVDA", "USDT", "TRADING", "TRADIFI_PERPETUAL"),
        _binance("NEWUSDT", "NEW", "USDT", "PENDING_TRADING", "PERPETUAL"),
        _binance("BTCUSDT_261225", "BTC", "USDT", "TRADING", "CURRENT_QUARTER"),
        _binance("BTCUSDC", "BTC", "USDC", "TRADING", "PERPETUAL"),
        _binance("OLDUSDT", "OLD", "USDT", "SETTLING", "PERPETUAL"),
        _binance("GONEUSDT", "GONE", "USDT", "CLOSE", "PERPETUAL"),
    ]}})
    scraper = BinanceFuturesScraper(http)
    assert Capability.MARKETS in scraper.capabilities
    assert await scraper.fetch_markets() == [
        Listing("BTC/USDT", "BTC"), Listing("NVDA/USDT", "NVDA"), Listing("NEW/USDT", "NEW"),
    ]


@pytest.mark.asyncio
async def test_bullet_lists_trading_perps_as_configured() -> None:
    http = RoutingHTTP({"/fapi/v1/exchangeInfo": {"symbols": [
        _binance("BTC-USD", "BTC", "USD", "TRADING", "CryptoPerp"),
        _binance("GOLD-USD", "GOLD", "USD", "TRADING", "RwaPerpCommodities"),
        _binance("OLD-USD", "OLD", "USD", "CLOSE", "CryptoPerp"),
    ]}})
    scraper = BulletScraper(http)
    assert Capability.MARKETS in scraper.capabilities
    assert await scraper.fetch_markets() == [
        Listing("BTC-USD", "BTC"), Listing("GOLD-USD", "GOLD"),
    ]


def _bybit(symbol: str, base: str, quote: str, status: str, contract: str | None = None) -> dict:
    row = {"symbol": symbol, "baseCoin": base, "quoteCoin": quote, "status": status}
    if contract is not None:
        row["contractType"] = contract
    return row


def _ok(rows: list[dict], cursor: str = "") -> dict:
    return {"retCode": 0, "result": {"list": rows, "nextPageCursor": cursor}}


@pytest.mark.asyncio
async def test_bybit_perp_pages_and_adds_pre_launch() -> None:
    def instruments(params: dict[str, str]) -> dict:
        assert params["category"] == "linear" and params["limit"] == "1000"
        if params.get("status") == "PreLaunch":
            return _ok([_bybit("NEWUSDT", "NEW", "USDT", "PreLaunch", "LinearPerpetual")])
        if "cursor" not in params:
            return _ok([
                _bybit("BTCUSDT", "BTC", "USDT", "Trading", "LinearPerpetual"),
                _bybit("BTCUSDT-26DEC26", "BTC", "USDT", "Trading", "LinearFutures"),
            ], cursor="page2")
        assert params["cursor"] == "page2"
        return _ok([
            _bybit("BTCPERP", "BTC", "USDC", "Trading", "LinearPerpetual"),
            _bybit("OLDUSDT", "OLD", "USDT", "Closed", "LinearPerpetual"),
            _bybit("1000PEPEUSDT", "1000PEPE", "USDT", "Trading", "LinearPerpetual"),
        ])

    http = RoutingHTTP({"/v5/market/instruments-info": instruments})
    scraper = BybitPerpScraper(http)
    assert Capability.MARKETS in scraper.capabilities
    assert await scraper.fetch_markets() == [
        Listing("BTC/USDT", "BTC"),
        Listing("1000PEPE/USDT", "1000PEPE"),
        Listing("NEW/USDT", "NEW"),
    ]
    assert len(http.calls) == 3


@pytest.mark.asyncio
async def test_bybit_spot_lists_usdt_pairs_in_one_request() -> None:
    http = RoutingHTTP({"/v5/market/instruments-info": _ok([
        _bybit("BTCUSDT", "BTC", "USDT", "Trading"),
        _bybit("BTCUSDC", "BTC", "USDC", "Trading"),
        _bybit("OLDUSDT", "OLD", "USDT", "Closed"),
    ])})
    scraper = BybitSpotScraper(http)
    assert Capability.MARKETS in scraper.capabilities
    assert await scraper.fetch_markets() == [Listing("BTC/USDT", "BTC")]
    assert http.calls == [
        ("https://api.bybit.com/v5/market/instruments-info",
         {"category": "spot", "limit": "1000"}),
    ]
