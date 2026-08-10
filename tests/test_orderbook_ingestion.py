from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from core.enums import Exchange, MarketType, QuoteCurrency
from core.models import TopOfBook
from scheduler.jobs import scrape_orderbook
from scheduler.targets import OrderBookTarget, load_targets


class _BookScraper:
    exchange = Exchange.BINANCE

    @staticmethod
    def market_type_for(symbol: str) -> MarketType:
        assert symbol == "BTC/USDT"
        return MarketType.PERP

    async def fetch_bbo(self, symbol: str) -> TopOfBook:
        return TopOfBook(
            exchange=self.exchange,
            symbol=symbol,
            quote_currency=QuoteCurrency.USDT,
            ts=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
            bid_price=Decimal("100.00"),
            bid_size=Decimal("2.5"),
            ask_price=Decimal("100.02"),
            ask_size=Decimal("3.0"),
        )


class _BookStorage:
    def __init__(self) -> None:
        self.records = []

    async def write_orderbook_snapshots(self, records) -> int:
        self.records.extend(records)
        return len(records)


@pytest.mark.asyncio
async def test_orderbook_job_persists_market_identity_and_quote() -> None:
    storage = _BookStorage()
    target = OrderBookTarget("binance_perp", "BTC/USDT")

    outcome = await scrape_orderbook(_BookScraper(), storage, target)

    assert outcome.status == "ok"
    assert outcome.fetched == outcome.new_rows == 1
    assert len(storage.records) == 1
    snapshot = storage.records[0]
    assert snapshot.exchange == Exchange.BINANCE
    assert snapshot.market_type == MarketType.PERP
    assert snapshot.symbol == "BTC/USDT"
    assert snapshot.bid_price == Decimal("100.00")
    assert snapshot.ask_price == Decimal("100.02")


def test_orderbook_config_builds_one_target_per_market(tmp_path) -> None:
    config = tmp_path / "symbols.toml"
    config.write_text(
        """
[venues.binance_spot]
intervals = ["1m"]
orderbook = true
orderbook_poll_seconds = 45

[assets.BTC]
binance_spot = "BTC/USDT"
"""
    )

    *_, orderbooks = load_targets(config)

    assert orderbooks == [OrderBookTarget("binance_spot", "BTC/USDT", 45)]
