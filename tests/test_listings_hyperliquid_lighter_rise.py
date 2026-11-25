from __future__ import annotations

from typing import Any

import pytest

from core.models import Listing
from data_collection.base import Capability, UnsupportedCapability
from data_collection.exchanges.hyperliquid import (
    HyperliquidDeployerScraper,
    HyperliquidScraper,
    HyperliquidXyzScraper,
)
from data_collection.exchanges.lighter import LighterScraper
from data_collection.exchanges.registry import REGISTRY
from data_collection.exchanges.risex import RiseScraper


class FakeHTTP:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        self.calls.append((url, kwargs))
        return self.response

    async def post_json(self, url: str, **kwargs: Any) -> Any:
        self.calls.append((url, kwargs))
        return self.response


def test_listing_venues_declare_markets_capability() -> None:
    for venue in ("hyperliquid", "hyperliquid_xyz", "hyperliquid_io", "lighter", "rise"):
        assert Capability.MARKETS in REGISTRY[venue].capabilities
    assert REGISTRY["hyperliquid_xyz"].dex == "xyz"
    assert REGISTRY["hyperliquid_io"].dex == "io"
    assert Capability.MARKETS not in HyperliquidDeployerScraper.capabilities


@pytest.mark.asyncio
async def test_hyperliquid_native_skips_delisted_and_uppercases_base() -> None:
    http = FakeHTTP({"universe": [
        {"name": "BTC"},
        {"name": "kPEPE"},
        {"name": "MATIC", "isDelisted": True},
    ]})
    listings = await HyperliquidScraper(http).fetch_markets()

    assert listings == [Listing("BTC", "BTC"), Listing("kPEPE", "KPEPE")]
    assert [kwargs["json"] for _, kwargs in http.calls] == [{"type": "meta"}]


@pytest.mark.asyncio
async def test_hyperliquid_deployer_lists_its_dex_without_prefix_in_base() -> None:
    http = FakeHTTP({"universe": [
        {"name": "xyz:GOLD"},
        {"name": "xyz:NVDA"},
        {"name": "xyz:VIX", "isDelisted": True},
    ]})
    listings = await HyperliquidXyzScraper(http).fetch_markets()

    assert listings == [Listing("xyz:GOLD", "GOLD"), Listing("xyz:NVDA", "NVDA")]
    assert [kwargs["json"] for _, kwargs in http.calls] == [{"type": "meta", "dex": "xyz"}]

    with pytest.raises(UnsupportedCapability):
        await HyperliquidDeployerScraper(FakeHTTP({})).fetch_markets()


@pytest.mark.asyncio
async def test_lighter_lists_active_perps_only() -> None:
    http = FakeHTTP({
        "order_book_details": [
            {"symbol": "XAU", "market_type": "perp", "status": "active",
             "market_config": {"force_reduce_only": False}},
            {"symbol": "ASELS", "market_type": "perp", "status": "active",
             "market_config": {"force_reduce_only": False}},
            {"symbol": "ASELS", "market_type": "perp", "status": "inactive",
             "market_config": {"force_reduce_only": False}},
            {"symbol": "MKR", "market_type": "perp", "status": "inactive",
             "market_config": {"force_reduce_only": False}},
            {"symbol": "WDC", "market_type": "perp", "status": "active",
             "market_config": {"force_reduce_only": True}},
        ],
        "spot_order_book_details": [
            {"symbol": "ETH/USDC", "market_type": "spot", "status": "active"},
        ],
    })
    listings = await LighterScraper(http).fetch_markets()

    assert listings == [Listing("XAU", "XAU"), Listing("ASELS", "ASELS")]
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_risex_lists_live_usdc_markets() -> None:
    def market(name: str, *, active: bool = True, unlocked: bool = True) -> dict:
        return {"active": active, "reduce_only": False,
                "config": {"name": name, "unlocked": unlocked}}

    http = FakeHTTP({"data": {"markets": [
        market("NVDA/USDC"),
        market("1000PEPE/USDC"),
        market("DOGE/USDC [deprecated-1779958099]", active=False),
        market("SNDK/USDC", active=False, unlocked=False),
        market("ETH/USDC", unlocked=False),
    ]}})
    listings = await RiseScraper(http).fetch_markets()

    assert listings == [Listing("NVDA/USDC", "NVDA"), Listing("1000PEPE/USDC", "1000PEPE")]
    assert len(http.calls) == 1
