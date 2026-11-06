from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from core.enums import Timeframe
from data_collection.exchanges.phoenix import PhoenixScraper


class PhoenixHTTP:
    """Routes by path; candle requests answer one bar per minute requested."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = routes or {}
        self.candle_windows: list[tuple[int, int]] = []

    async def get_json(self, url: str, *, params: dict[str, str] | None = None, **_: Any) -> Any:
        if "/v1/candles/" in url:
            start, end = int(params["startTime"]), int(params["endTime"])
            self.candle_windows.append((start, end))
            first = -(-start // 60_000) * 60_000
            return [{"time": t, "open": 1.5, "high": 2.0, "low": 1.0, "close": 1.25, "volume": 3.0}
                    for t in range(first, end + 1, 60_000)]
        for path, body in self.routes.items():
            if path in url:
                return body
        raise AssertionError(f"unexpected request: {url}")


@pytest.mark.asyncio
async def test_funding_percentages_are_stored_as_hourly_fractions():
    hour = datetime(2026, 11, 4, 22, tzinfo=timezone.utc)
    scraper = PhoenixScraper(PhoenixHTTP({"/v1/funding/BTC/rates": {"symbol": "BTC", "rates": [
        {"timestamp": int(hour.timestamp()) - 3600, "fundingRatePercentage": "0.001266"},
        {"timestamp": int(hour.timestamp()), "fundingRatePercentage": "0.001250"},
    ]}}))

    rows = await scraper.fetch_funding("BTC", hour - timedelta(days=1))

    # Phoenix pays an eighth of its 8-hour rate each hour and lists it as a
    # percentage: 0.00125% an hour is the 0.01%-per-8-hours resting rate.
    assert [(row.ts, row.rate, row.interval_hours) for row in rows] == [
        (hour - timedelta(hours=1), Decimal("0.00001266"), 1),
        (hour, Decimal("0.0000125"), 1),
    ]
    assert rows[1].rate * 8760 == Decimal("0.1095")


@pytest.mark.asyncio
async def test_liquidity_reads_live_stats_with_percent_funding():
    scraper = PhoenixScraper(PhoenixHTTP())

    async def stats(symbols: list[str]) -> dict[str, dict]:
        return {"NVDA": {"channel": "market", "symbol": "NVDA", "openInterest": 324.346,
                         "markPx": 201.31, "oraclePx": 201.21, "dayNtlVlm": 43565.71,
                         "funding": 0.0036096898647194208}}

    scraper._market_stats = stats  # type: ignore[method-assign]
    before = datetime.now(timezone.utc)
    [nvda] = await scraper.fetch_liquidity(["NVDA", "GOLD"])     # GOLD sent nothing

    assert (nvda.symbol, nvda.open_interest, nvda.mark_price, nvda.index_price, nvda.volume_24h) == (
        "NVDA", Decimal("324.346"), Decimal("201.31"), Decimal("201.21"), Decimal("43565.71"))
    assert nvda.current_funding_rate == Decimal("0.000036096898647194208")
    assert nvda.funding_interval_hours == 1
    assert nvda.next_funding_at == before.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    async def silent(symbols: list[str]) -> dict[str, dict]:
        return {}

    scraper._market_stats = silent  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await scraper.fetch_liquidity(["NVDA"])


@pytest.mark.asyncio
async def test_candle_history_is_paged_within_the_2500_candle_limit():
    http = PhoenixHTTP()
    scraper = PhoenixScraper(http)
    since = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=3000)

    bars = await scraper.fetch_ohlcv("BTC", Timeframe.M1, since)

    assert len(http.candle_windows) == 2
    assert all((end - start) // 60_000 + 1 <= 2500 for start, end in http.candle_windows)
    minutes = [bar.ts for bar in bars]
    assert minutes == sorted(set(minutes))                   # page edges not doubled
    assert minutes[0] == since and len(minutes) >= 3000
    assert (bars[0].open, bars[0].close, bars[0].volume) == (Decimal("1.5"), Decimal("1.25"), Decimal("3.0"))
