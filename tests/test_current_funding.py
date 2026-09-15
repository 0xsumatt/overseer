from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from data_collection.exchanges.binance import BinanceFuturesScraper
from data_collection.exchanges.bullet import BulletScraper
from data_collection.exchanges.bulk import BulkScraper
from data_collection.exchanges.bybit import BybitPerpScraper
from data_collection.exchanges.extended import ExtendedScraper
from data_collection.exchanges.hyperliquid import HyperliquidScraper
from data_collection.exchanges.lighter import LighterScraper
from data_collection.exchanges.risex import RiseScraper
from core.enums import Timeframe


class RoutingHTTP:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses

    def _response(self, url: str) -> Any:
        for marker, response in self.responses.items():
            if marker in url:
                return response
        raise AssertionError(f"unexpected request: {url}")

    async def get_json(self, url: str, **_: Any) -> Any:
        return self._response(url)

    async def post_json(self, url: str, **_: Any) -> Any:
        return self._response(url)


def assert_current(
    row: Any, *, rate: str, index: str, hours: int, has_next: bool = True
) -> None:
    assert row.current_funding_rate == Decimal(rate)
    assert row.index_price == Decimal(index)
    assert row.funding_interval_hours == hours
    assert (row.next_funding_at is not None) is has_next


@pytest.mark.asyncio
async def test_centralized_and_compatible_tickers_publish_current_funding() -> None:
    binance = BinanceFuturesScraper(
        RoutingHTTP(
            {
                "/openInterest": {"openInterest": "10"},
                "/ticker/24hr": {"quoteVolume": "1000"},
                "/premiumIndex": {
                    "markPrice": "100.5",
                    "indexPrice": "100.0",
                    "lastFundingRate": "0.0002",
                    "nextFundingTime": 1_700_000_000_000,
                },
                "/fundingInfo": [
                    {"symbol": "BTCUSDT", "fundingIntervalHours": 4}
                ],
            }
        )
    )
    [row] = await binance.fetch_liquidity(["BTC/USDT"])
    assert_current(row, rate="0.0002", index="100.0", hours=4)

    bybit = BybitPerpScraper(
        RoutingHTTP(
            {
                "/v5/market/tickers": {
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "openInterest": "10",
                                "turnover24h": "1000",
                                "markPrice": "100.5",
                                "indexPrice": "100.0",
                                "fundingRate": "-0.0003",
                                "fundingIntervalHour": "8",
                                "nextFundingTime": "1700000000000",
                            }
                        ]
                    },
                }
            }
        )
    )
    [row] = await bybit.fetch_liquidity(["BTC/USDT"])
    assert_current(row, rate="-0.0003", index="100.0", hours=8)

    bullet = BulletScraper(
        RoutingHTTP(
            {
                "/openInterest": [{"symbol": "BTC-USD", "openInterest": "10"}],
                "/ticker/24hr": [{"symbol": "BTC-USD", "quoteVolume": "1000"}],
                "/premiumIndex": [
                    {
                        "symbol": "BTC-USD",
                        "markPrice": "100.5",
                        "indexPrice": "100.0",
                        "lastFundingRate": "0.0009",
                        "estimatedFundingRate": "0.0004",
                        "nextFundingTime": 1_700_000_000_000,
                    }
                ],
                "/fundingInfo": [
                    {"symbol": "BTC-USD", "fundingIntervalHours": 1}
                ],
            }
        )
    )
    [row] = await bullet.fetch_liquidity(["BTC-USD"])
    assert_current(row, rate="0.0004", index="100.0", hours=1)


@pytest.mark.asyncio
async def test_hyperliquid_context_preserves_funding_inputs() -> None:
    scraper = HyperliquidScraper(
        RoutingHTTP(
            {
                "/info": [
                    {"universe": [{"name": "BTC"}]},
                    [
                        {
                            "openInterest": "10",
                            "dayNtlVlm": "1000",
                            "markPx": "100.5",
                            "oraclePx": "100.0",
                            "funding": "0.0002",
                            "premium": "0.00015",
                        }
                    ],
                ]
            }
        )
    )
    [row] = await scraper.fetch_liquidity(["BTC"])
    assert_current(row, rate="0.0002", index="100.0", hours=1)
    assert row.funding_premium == Decimal("0.00015")


@pytest.mark.asyncio
async def test_dex_market_payloads_publish_current_funding() -> None:
    lighter = LighterScraper(
        RoutingHTTP(
            {
                "/orderBookDetails": {
                    "order_book_details": [
                        {
                            "symbol": "BTC",
                            "market_id": 1,
                            "open_interest": "10",
                            "daily_quote_token_volume": "1000",
                            "mark_price": "100.5",
                            "index_price": "100.0",
                        }
                    ],
                    "spot_order_book_details": [],
                },
                "/funding-rates": {
                    "funding_rates": [
                        {
                            "market_id": 1,
                            "exchange": "lighter",
                            "symbol": "BTC",
                            "rate": "0.000096",
                        },
                        {
                            "market_id": 1,
                            "exchange": "binance",
                            "symbol": "BTC",
                            "rate": "0.9",
                        },
                    ]
                },
            }
        )
    )
    [row] = await lighter.fetch_liquidity(["BTC"])
    assert_current(row, rate="0.000012", index="100.0", hours=1)

    extended = ExtendedScraper(
        RoutingHTTP(
            {
                "/api/v1/info/markets": {
                    "status": "OK",
                    "data": [
                        {
                            "name": "BTC-USD",
                            "type": "PERPETUAL",
                            "marketStats": {
                                "openInterestBase": "10",
                                "dailyVolume": "1000",
                                "markPrice": "100.5",
                                "indexPrice": "100.0",
                                "fundingRate": "0.0003",
                                "nextFundingRate": 1_700_000_000_000,
                            },
                        }
                    ],
                }
            }
        )
    )
    [row] = await extended.fetch_liquidity(["BTC-USD"])
    assert_current(row, rate="0.0003", index="100.0", hours=1)

    rise = RiseScraper(
        RoutingHTTP(
            {
                "/v1/markets": {
                    "data": {
                        "markets": [
                            {
                                "market_id": "1",
                                "config": {"name": "BTC/USDC", "unlocked": True},
                                "active": True,
                                "open_interest": "10",
                                "quote_volume_24h": "1000",
                                "mark_price": "100.5",
                                "index_price": "100.0",
                                "current_funding_rate": "-0.0004",
                                "funding_interval": str(3_600_000_000_000),
                                "next_funding_time": str(1_700_000_000_000_000_000),
                            }
                        ]
                    }
                }
            }
        )
    )
    [row] = await rise.fetch_liquidity(["BTC/USDC"])
    assert_current(row, rate="-0.0004", index="100.0", hours=1)

    bulk = BulkScraper(
        RoutingHTTP(
            {
                "/ticker/BTC-USD": {
                    "openInterest": "10",
                    "quoteVolume": "1000",
                    "markPrice": "100.5",
                    "oraclePrice": "100.0",
                    "fundingRate": "0.0005",
                }
            }
        )
    )
    [row] = await bulk.fetch_liquidity(["BTC-USD"])
    assert_current(row, rate="0.0005", index="100.0", hours=1, has_next=False)


@pytest.mark.asyncio
async def test_lighter_settled_funding_converts_percent_to_fraction() -> None:
    scraper = LighterScraper(
        RoutingHTTP(
            {
                "/orderBookDetails": {
                    "order_book_details": [
                        {"symbol": "BTC", "market_id": 1}
                    ],
                    "spot_order_book_details": [],
                },
                "/fundings": {
                    "fundings": [
                        {
                            "timestamp": 1_700_000_000,
                            "rate": "0.0008",
                            "direction": "long",
                        },
                        {
                            "timestamp": 1_700_003_600,
                            "rate": "0.0004",
                            "direction": "short",
                        },
                    ]
                },
            }
        )
    )

    rows = await scraper.fetch_funding(
        "BTC", datetime(2023, 1, 1, tzinfo=timezone.utc)
    )

    assert [row.rate for row in rows] == [
        Decimal("0.000008"),
        Decimal("-0.000004"),
    ]
    assert [row.interval_hours for row in rows] == [1, 1]


@pytest.mark.asyncio
async def test_bullet_funding_defaults_to_hourly_when_info_is_empty() -> None:
    scraper = BulletScraper(
        RoutingHTTP(
            {
                "/fundingRate": [
                    {
                        "symbol": "BTC-USD",
                        "fundingRate": "0.000048",
                        "fundingTime": 1_700_000_000_000_000,
                    }
                ],
                "/fundingInfo": [],
            }
        )
    )

    [row] = await scraper.fetch_funding(
        "BTC-USD", datetime(2023, 1, 1, tzinfo=timezone.utc)
    )

    assert row.rate == Decimal("0.000048")
    assert row.interval_hours == 1


@pytest.mark.asyncio
async def test_bulk_ohlcv_keeps_one_candle_per_open_time() -> None:
    scraper = BulkScraper(
        RoutingHTTP(
            {
                "/klines": [
                    {
                        "t": 1_700_000_000_000,
                        "T": 1_700_000_060_000,
                        "o": "100",
                        "h": "102",
                        "l": "99",
                        "c": "101",
                        "v": "4",
                        "n": 12,
                    },
                    {
                        "t": 1_700_000_000_000,
                        "T": 1_700_000_060_000,
                        "o": "101",
                        "h": "101",
                        "l": "101",
                        "c": "101",
                        "v": "0",
                        "n": 0,
                    },
                ]
            }
        )
    )

    rows = await scraper.fetch_ohlcv(
        "BTC-USD", Timeframe.M1, datetime(2023, 1, 1, tzinfo=timezone.utc)
    )

    assert len(rows) == 1
    assert rows[0].volume == Decimal("4")
    assert rows[0].high == Decimal("102")
