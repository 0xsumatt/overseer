from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from data_collection.exchanges.qfex import QfexScraper


class QfexHTTP:
    """Routes by path and fails any request that is not pinned to a contract."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes

    async def get_json(self, url: str, *, params: dict[str, str] | None = None, **_: Any) -> Any:
        assert (params or {}).get("api_version") == QfexScraper.API_VERSION, url
        for path, body in self.routes.items():
            if path in url:
                return body
        raise AssertionError(f"unexpected request: {url}")


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def window(start: datetime, rate: float | None) -> dict[str, Any]:
    return {"symbol": "AAPL-USD", "intervalMinutes": 60, "windowStart": iso(start), "rate": rate}


@pytest.mark.asyncio
async def test_settled_funding_is_keyed_by_payment_time_and_waits_five_minutes():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    scraper = QfexScraper(QfexHTTP({"/funding/AAPL-USD": {"data": [
        window(now - timedelta(hours=3), -3.1e-07),
        window(now - timedelta(hours=2), 0.00001),
        window(now - timedelta(hours=1), None),
        window(now - timedelta(minutes=3), 0.00002),  # paid three minutes ago
    ]}}))

    rows = await scraper.fetch_funding("AAPL-USD", now - timedelta(days=1))

    # QFEX's windowStart is the payment time; a settlement counts once five
    # minutes old, and rates are exact fractions even in exponent form.
    assert [(row.ts, row.rate) for row in rows] == [
        (now - timedelta(hours=3), Decimal("-3.1E-7")),
        (now - timedelta(hours=2), Decimal("0.00001")),
    ]
    assert {row.interval_hours for row in rows} == {1}


def contract(ticker: str, **changes: Any) -> dict[str, Any]:
    return {
        "ticker_id": ticker, "target_currency": "USD", "funding_interval_minutes": 60,
        "open_interest": "10", "open_interest_usd": "1000", "index_price": "100",
        "last_price": "101", "target_volume": "5000",
        "funding_rate": "0.00005", "next_funding_rate": "0.00002", **changes,
    }


@pytest.mark.asyncio
async def test_current_funding_and_open_interest_follow_qfex_definitions():
    scraper = QfexScraper(QfexHTTP({"/md/contracts": {"data": [
        contract("GOLD-USD"),
        contract("CXMT-CNY", target_currency="CNY"),
        contract("HALF-USD", funding_interval_minutes=30),
    ]}}))

    rows = {row.symbol: row for row in await scraper.fetch_liquidity(
        ["GOLD-USD", "CXMT-CNY", "HALF-USD", "UNLISTED-USD"])}

    gold = rows["GOLD-USD"]
    # The next settlement's predicted rate, not the last one.
    assert (gold.current_funding_rate, gold.funding_interval_hours) == (Decimal("0.00002"), 1)
    assert gold.next_funding_at.minute == 0 and gold.next_funding_at > gold.ts
    # Open interest is valued at the index, as QFEX values it, not the last trade.
    assert gold.mark_price == gold.index_price == Decimal("100")
    assert (gold.open_interest, gold.volume_24h) == (Decimal("10"), Decimal("5000"))
    # Local-currency markets would mix KRW or CNY volume with USD.
    assert "CXMT-CNY" not in rows and "UNLISTED-USD" not in rows
    # An interval of part of an hour cannot be expressed in whole hours.
    assert rows["HALF-USD"].current_funding_rate is None


@pytest.mark.asyncio
async def test_top_of_book_skips_emptied_levels():
    scraper = QfexScraper(QfexHTTP({"/md/orderbook/AAPL-USD": {
        "ticker_id": "AAPL-USD", "timestamp": 1_793_448_190_015,
        "bids": [["100.2", "0"], ["100.1", "2"], ["99", "5"]],
        "asks": [["100.3", "0"], ["100.4", "1"]],
    }}))

    book = await scraper.fetch_bbo("AAPL-USD")

    assert (book.bid_price, book.bid_size) == (Decimal("100.1"), Decimal("2"))
    assert (book.ask_price, book.ask_size) == (Decimal("100.4"), Decimal("1"))
    assert book.ts == datetime.fromtimestamp(1_793_448_190.015, tz=timezone.utc)


def day(start: datetime, volume: float) -> dict[str, Any]:
    return {"intervalMinutes": 1440, "windowStart": iso(start),
            "dailyVolumeUSD": volume, "openInterestAtEndUSD": 1.0}


@pytest.mark.asyncio
async def test_venue_volume_is_the_last_complete_day():
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    scraper = QfexScraper(QfexHTTP({"/defillama/metrics": {"data": [
        day(today - timedelta(days=2), 400.0), day(today - timedelta(days=1), 500.0), day(today, 999.0),
    ]}}))

    assert await scraper.fetch_venue_volume() == {"spot": None, "perp": Decimal("500.0")}
