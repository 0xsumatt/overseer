from __future__ import annotations

from typing import Any

import pytest

from core.models import Listing
from data_collection.base import Capability
from data_collection.exchanges.bulk import BulkScraper
from data_collection.exchanges.extended import ExtendedScraper
from data_collection.exchanges.phoenix import PhoenixScraper
from data_collection.exchanges.qfex import QfexScraper


class FakeHTTP:
    """Answers one path with a fixed body and records every request."""

    def __init__(self, path: str, body: Any) -> None:
        self.path, self.body = path, body
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    async def get_json(self, url: str, *, params: dict[str, str] | None = None, **_: Any) -> Any:
        self.calls.append((url, params))
        assert url.endswith(self.path), url
        return self.body


@pytest.mark.asyncio
async def test_extended_keeps_active_and_prelisted_perps_and_drops_session_suffix():
    def market(name: str, asset: str, status: str, kind: str = "PERPETUAL") -> dict[str, Any]:
        return {"name": name, "assetName": asset, "status": status, "type": kind,
                "collateralAssetName": "USD"}

    http = FakeHTTP("/api/v1/info/markets", {"status": "OK", "data": [
        market("BTC-USD", "BTC", "ACTIVE"),
        market("AAPL_24_5-USD", "AAPL_24_5", "ACTIVE"),
        market("SPX500m-USD", "SPX500m", "ACTIVE"),
        market("PYTH-USD", "PYTH", "PRELISTED"),
        market("XYZ-USD", "XYZ", "REDUCE_ONLY"),
        market("NOW_24_5-USD", "NOW_24_5", "DELISTED"),
        market("BTCSPOT-USD", "BTCSPOT", "ACTIVE", kind="SPOT"),
    ]})

    listings = await ExtendedScraper(http).fetch_markets()

    assert Capability.MARKETS in ExtendedScraper.capabilities
    assert listings == [
        Listing(symbol="BTC-USD", base="BTC"),
        Listing(symbol="AAPL_24_5-USD", base="AAPL"),
        Listing(symbol="SPX500m-USD", base="SPX500M"),
        Listing(symbol="PYTH-USD", base="PYTH"),
    ]
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_bulk_lists_only_trading_usd_markets():
    def market(symbol: str, status: str) -> dict[str, Any]:
        return {"symbol": symbol, "baseAsset": symbol.split("-")[0], "quoteAsset": "USD",
                "status": status}

    http = FakeHTTP("/exchangeInfo", [
        market("AAPL-USD", "TRADING"), market("AAVE-USD", "CLOSED"),
        market("META-USD", "SUSPENDED"), market("NVDA-USD", "TRADING"),
    ])

    listings = await BulkScraper(http).fetch_markets()

    assert Capability.MARKETS in BulkScraper.capabilities
    assert listings == [Listing("AAPL-USD", "AAPL"), Listing("NVDA-USD", "NVDA")]


@pytest.mark.asyncio
async def test_qfex_lists_active_usd_symbols_from_pinned_refdata():
    def row(symbol: str, status: str) -> dict[str, Any]:
        base, quote = symbol.split("-")
        return {"symbol": symbol, "base_asset": base, "quote_asset": quote, "status": status}

    http = FakeHTTP("/refdata", {"type": "refdata", "data": [
        row("GOLD-USD", "ACTIVE"), row("ADBE-USD", "INACTIVE"), row("EUR-USD", "DELISTED"),
        row("SAMSUNG-KRW", "ACTIVE"), row("US500-USD", "ACTIVE"),
    ]})

    listings = await QfexScraper(http).fetch_markets()

    assert Capability.MARKETS in QfexScraper.capabilities
    assert listings == [Listing("GOLD-USD", "GOLD"), Listing("US500-USD", "US500")]
    assert http.calls[0][1] == {"api_version": QfexScraper.API_VERSION}


@pytest.mark.asyncio
async def test_phoenix_drops_closed_and_tombstoned_markets():
    http = FakeHTTP("/v1/view/exchange/markets", [
        {"symbol": "GOLD", "marketStatus": "active"},
        {"symbol": "ANTHROPIC", "marketStatus": "closed"},
        {"symbol": "OLD", "marketStatus": "tombstoned"},
        {"symbol": "WTIOIL", "marketStatus": "active"},
    ])

    listings = await PhoenixScraper(http).fetch_markets()

    assert Capability.MARKETS in PhoenixScraper.capabilities
    assert listings == [Listing("GOLD", "GOLD"), Listing("WTIOIL", "WTIOIL")]
