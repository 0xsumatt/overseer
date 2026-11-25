from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from core.enums import Exchange, MarketType, Timeframe
from core.models import OHLCV
from data_collection.exchanges.binance import BinanceFuturesScraper
from data_collection.exchanges.extended import ExtendedScraper
from storage.writers import Storage


class Http:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[dict[str, str], float]] = []

    async def get_json(self, url: str, *, params: dict[str, str], weight: float = 1.0) -> Any:
        self.calls.append((params, weight))
        return self.payload

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_candle_polls_ask_only_for_the_bars_since_the_last_stored_one() -> None:
    now = datetime.now(timezone.utc)
    extended_http = Http({"status": "OK", "data": []})
    binance_http = Http([])
    extended = ExtendedScraper(extended_http)  # type: ignore[arg-type]
    binance = BinanceFuturesScraper(binance_http)  # type: ignore[arg-type]

    for since in (now - timedelta(seconds=90), now - timedelta(days=3)):
        await extended.fetch_ohlcv("BTC-USD", Timeframe.M1, since)
        await binance.fetch_ohlcv("BTC/USDT", Timeframe.M1, since)

    # 90 s behind: the stored bar, the current one and a spare. Binance charges
    # futures klines by the limit asked for: 1 under 100 bars, 5 for 1,000.
    assert [params["limit"] for params, _ in extended_http.calls] == ["3", "1000"]
    assert [(params["limit"], weight) for params, weight in binance_http.calls] == [
        ("3", 1), ("1000", 5),
    ]


@pytest.mark.asyncio
async def test_refetched_bars_are_rewritten_only_when_revised() -> None:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for the PostgreSQL upsert regression")
    schema = f"ohlcv_upsert_{uuid4().hex}"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        await connection.execute(f"SET search_path TO {schema}")
        # The columns 001 creates, as a plain table: hypertables need TimescaleDB.
        await connection.execute(
            """
            CREATE TABLE ohlcv (
                exchange text NOT NULL, market_type text NOT NULL, symbol text NOT NULL,
                interval text NOT NULL, ts timestamptz NOT NULL, open numeric NOT NULL,
                high numeric NOT NULL, low numeric NOT NULL, close numeric NOT NULL,
                volume numeric NOT NULL,
                PRIMARY KEY (exchange, market_type, symbol, interval, ts))
            """
        )
        storage = Storage(dsn)
        storage._pool = connection  # type: ignore[assignment]
        bar = OHLCV(Exchange.BINANCE, MarketType.PERP, "BTC/USDT", Timeframe.M1,
                    datetime(2026, 1, 1, tzinfo=timezone.utc), Decimal("100"),
                    Decimal("101"), Decimal("99"), Decimal("100.5"), Decimal("7"))

        assert await storage.write_ohlcv([bar]) == 1
        version = await connection.fetchval("SELECT xmin::text FROM ohlcv")
        # The same bar again (every poll re-reads the latest stored one).
        assert await storage.write_ohlcv([bar]) == 0
        assert await connection.fetchval("SELECT xmin::text FROM ohlcv") == version
        # A revised bar is still corrected, and is not counted as new.
        assert await storage.write_ohlcv([replace(bar, close=Decimal("100.7"), volume=Decimal("9"))]) == 0
        assert tuple(await connection.fetchrow("SELECT close, volume FROM ohlcv")) == (
            Decimal("100.7"), Decimal("9"),
        )
    finally:
        await connection.execute("RESET search_path")
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()
