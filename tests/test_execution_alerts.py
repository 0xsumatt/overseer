from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from core.enums import Exchange, MarketType, QuoteCurrency
from core.models import TopOfBook
from core.symbols import SymbolRegistry
from data_collection.base import Capability
from data_collection.exchanges.binance import BinanceFuturesScraper
from data_collection.exchanges.bullet import BulletScraper
from data_collection.exchanges.bybit import BybitPerpScraper
from data_collection.exchanges.extended import ExtendedScraper
from data_collection.exchanges.hyperliquid import HyperliquidScraper
from data_collection.exchanges.lighter import LighterScraper
from data_collection.exchanges.risex import RiseScraper
from scheduler.jobs import UsdcUsdtQuoteCache, check_dislocations


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


@pytest.mark.asyncio
async def test_enabled_perp_adapters_parse_executable_top_of_book() -> None:
    binance = BinanceFuturesScraper(
        RoutingHTTP(
            {
                "/bookTicker": {
                    "bidPrice": "100.1",
                    "bidQty": "2.1",
                    "askPrice": "100.2",
                    "askQty": "3.2",
                }
            }
        )
    )
    bybit = BybitPerpScraper(
        RoutingHTTP(
            {
                "/tickers": {
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "bid1Price": "100.1",
                                "bid1Size": "2.1",
                                "ask1Price": "100.2",
                                "ask1Size": "3.2",
                            }
                        ]
                    },
                }
            }
        )
    )
    hyperliquid = HyperliquidScraper(
        RoutingHTTP(
            {
                "/info": {
                    "levels": [
                        [{"px": "100.1", "sz": "2.1"}],
                        [{"px": "100.2", "sz": "3.2"}],
                    ]
                }
            }
        )
    )
    lighter = LighterScraper(
        RoutingHTTP(
            {
                "/orderBookDetails": {
                    "order_book_details": [{"symbol": "BTC", "market_id": 1}],
                    "spot_order_book_details": [],
                },
                "/orderBookOrders": {
                    "bids": [{"price": "100.1", "remaining_base_amount": "2.1"}],
                    "asks": [{"price": "100.2", "remaining_base_amount": "3.2"}],
                },
            }
        )
    )
    extended = ExtendedScraper(
        RoutingHTTP(
            {
                "/orderbook": {
                    "status": "OK",
                    "data": {
                        "bid": [{"price": "100.1", "qty": "2.1"}],
                        "ask": [{"price": "100.2", "qty": "3.2"}],
                    },
                }
            }
        )
    )
    rise = RiseScraper(
        RoutingHTTP(
            {
                "/v1/markets": {
                    "data": {
                        "markets": [
                            {
                                "market_id": "1",
                                "active": True,
                                "config": {"name": "BTC/USDC", "unlocked": True},
                            }
                        ]
                    }
                },
                "/v1/orderbook": {
                    "data": {
                        "bids": [{"price": "100.1", "quantity": "2.1"}],
                        "asks": [{"price": "100.2", "quantity": "3.2"}],
                    }
                },
            }
        )
    )
    bullet = BulletScraper(
        RoutingHTTP(
            {
                "/depth": {
                    "bids": [["100.1", "2.1"]],
                    "asks": [["100.2", "3.2"]],
                }
            }
        )
    )

    books = [
        await binance.fetch_bbo("BTC/USDT"),
        await bybit.fetch_bbo("BTC/USDT"),
        await hyperliquid.fetch_bbo("BTC"),
        await lighter.fetch_bbo("BTC"),
        await extended.fetch_bbo("BTC-USD"),
        await rise.fetch_bbo("BTC/USDC"),
        await bullet.fetch_bbo("BTC-USD"),
    ]

    assert all(book.bid_price == Decimal("100.1") for book in books)
    assert all(book.bid_size == Decimal("2.1") for book in books)
    assert all(book.ask_price == Decimal("100.2") for book in books)
    assert all(book.ask_size == Decimal("3.2") for book in books)
    assert books[0].quote_currency is QuoteCurrency.USDT
    assert all(book.quote_currency is QuoteCurrency.USDC for book in books[2:])


class FakeBookScraper:
    capabilities = frozenset({Capability.BBO})
    market_type = MarketType.PERP

    def __init__(self, exchange: Exchange, quote: QuoteCurrency, book: TopOfBook) -> None:
        self.exchange = exchange
        self.quote_currency = quote
        self.book = book

    @classmethod
    def market_type_for(cls, _: str) -> MarketType:
        return cls.market_type

    async def fetch_bbo(self, _: str) -> TopOfBook:
        return self.book


class AlertStorage:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def latest_current_funding_rows(self) -> list[dict[str, Any]]:
        return self.rows


class AlertNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def digest(self, text: str) -> None:
        self.messages.append(text)


class FixedFxCache:
    def __init__(self, book: TopOfBook) -> None:
        self.book = book

    async def get(self) -> tuple[TopOfBook, bool]:
        return self.book, False


@pytest.mark.asyncio
async def test_dislocation_alert_uses_current_rates_bbo_and_side_aware_fx() -> None:
    now = datetime.now(timezone.utc)
    binance_book = TopOfBook(
        Exchange.BINANCE,
        "BTC/USDT",
        QuoteCurrency.USDT,
        now,
        Decimal("100"),
        Decimal("2"),
        Decimal("101"),
        Decimal("3"),
    )
    hyperliquid_book = TopOfBook(
        Exchange.HYPERLIQUID,
        "BTC",
        QuoteCurrency.USDC,
        now,
        Decimal("99"),
        Decimal("4"),
        Decimal("99.5"),
        Decimal("5"),
    )
    fx_book = TopOfBook(
        Exchange.BINANCE,
        "USDC/USDT",
        QuoteCurrency.USDT,
        now,
        Decimal("1.001"),
        Decimal("1000000"),
        Decimal("1.002"),
        Decimal("1000000"),
    )
    rows = [
        {
            "exchange": "binance",
            "symbol": "BTC/USDT",
            "ts": now,
            "rate": Decimal("0.0005"),
            "interval_hours": 8,
        },
        {
            "exchange": "hyperliquid",
            "symbol": "BTC",
            "ts": now,
            "rate": Decimal("-0.0001"),
            "interval_hours": 1,
        },
    ]
    storage = AlertStorage(rows)
    notifier = AlertNotifier()
    state: dict[str, str] = {}
    registry = SymbolRegistry.from_config(
        {
            "assets": {
                "BTC": {
                    "binance_perp": "BTC/USDT",
                    "hyperliquid": "BTC",
                }
            }
        }
    )
    scrapers = {
        "binance_perp": FakeBookScraper(
            Exchange.BINANCE, QuoteCurrency.USDT, binance_book
        ),
        "hyperliquid": FakeBookScraper(
            Exchange.HYPERLIQUID, QuoteCurrency.USDC, hyperliquid_book
        ),
    }

    await check_dislocations(
        storage,
        notifier,
        state,
        registry,
        25,
        scrapers,  # type: ignore[arg-type]
        FixedFxCache(fx_book),  # type: ignore[arg-type]
    )
    await check_dislocations(
        storage,
        notifier,
        state,
        registry,
        25,
        scrapers,  # type: ignore[arg-type]
        FixedFxCache(fx_book),  # type: ignore[arg-type]
    )

    assert len(notifier.messages) == 1
    alert = notifier.messages[0]
    assert "current funding spread 142.3% APR" in alert
    assert "**SHORT**" in alert and "**LONG**" in alert
    assert "bid 100.000 × 2.000 | ask 101.000 × 3.000 USDT" in alert
    assert "bid 99.099 × 4.000 | ask 99.699 × 5.000 USDT" in alert
    assert "USDC/USDT 1.001000 / 1.002000" in alert
    assert "Gross entry edge: +30.1 bps" in alert
    assert "Fees and slippage beyond displayed size excluded" in alert
    assert state == {"dislocation:BTC": "wide"}

    storage.rows[0]["rate"] = Decimal("0.00001")
    storage.rows[1]["rate"] = Decimal("0.000001")
    await check_dislocations(
        storage,
        notifier,
        state,
        registry,
        25,
        scrapers,  # type: ignore[arg-type]
        FixedFxCache(fx_book),  # type: ignore[arg-type]
    )
    assert len(notifier.messages) == 2
    assert "current funding spread narrowed" in notifier.messages[1]
    assert state == {"dislocation:BTC": "ok"}


class FlakySpotScraper:
    capabilities = frozenset({Capability.BBO})

    def __init__(self, book: TopOfBook) -> None:
        self.book = book
        self.calls = 0

    async def fetch_bbo(self, _: str) -> TopOfBook:
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("temporary failure")
        return self.book


@pytest.mark.asyncio
async def test_fx_cache_uses_recent_quote_when_refresh_fails() -> None:
    now = datetime.now(timezone.utc)
    book = TopOfBook(
        Exchange.BINANCE,
        "USDC/USDT",
        QuoteCurrency.USDT,
        now,
        Decimal("1.001"),
        Decimal("1000000"),
        Decimal("1.002"),
        Decimal("1000000"),
    )
    scraper = FlakySpotScraper(book)
    cache = UsdcUsdtQuoteCache(
        scraper,  # type: ignore[arg-type]
        fresh_for=timedelta(0),
        fallback_for=timedelta(minutes=5),
    )

    first, first_cached = await cache.get()
    second, second_cached = await cache.get()

    assert first is book and first_cached is False
    assert second is book and second_cached is True
    assert scraper.calls == 2
