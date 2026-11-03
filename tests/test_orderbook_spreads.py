from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from storage.queries import ReadStorage


def test_orderbook_spreads_average_snapshots_per_bucket_on_postgres():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for the PostgreSQL query regression")
    schema = f"orderbook_spreads_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(f"CREATE SCHEMA {schema}")
    store = ReadStorage(make_conninfo(dsn, options=f"-csearch_path={schema}"), max_size=1)
    try:
        with psycopg.connect(dsn, autocommit=True, options=f"-csearch_path={schema}") as connection:
            # The columns 009 creates, as a plain table: hypertables need TimescaleDB.
            connection.execute(
                """
                CREATE TABLE orderbook_snapshots (
                    exchange text, market_type text, symbol text, quote_currency text,
                    ts timestamptz, bid_price numeric, bid_size numeric,
                    ask_price numeric, ask_size numeric);
                INSERT INTO orderbook_snapshots VALUES
                    ('binance', 'perp', 'BTC/USDT', 'USDT', '2026-11-03 10:01+00', 99.95, 1, 100.05, 1),
                    ('binance', 'perp', 'BTC/USDT', 'USDT', '2026-11-03 10:03+00', 99.9,  3, 100.1,  1),
                    ('binance', 'perp', 'BTC/USDT', 'USDT', '2026-11-03 10:06+00', 199.8, 1, 200.2,  1),
                    ('binance', 'perp', 'BTC/USDT', 'USDT', '2026-11-03 09:59+00', 1,     1, 2,      1),
                    ('binance', 'spot', 'BTC/USDT', 'USDT', '2026-11-03 10:01+00', 1,     1, 2,      1),
                    ('bybit',   'perp', 'BTC/USDT', 'USDT', '2026-11-03 10:01+00', 1,     1, 2,      1);
                """
            )

        rows = store.orderbook_spreads(
            "perp", ["BTC/USDT"], ["binance", "qfex"],
            datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc), 5,
        )

        # Five-minute buckets average their snapshots (10 and 20 bps around a
        # 100 mid); older rows, other markets and unrequested venues stay out.
        assert [
            (row["exchange"], row["ts"], float(row["spread_bps"]),
             float(row["midpoint"]), float(row["bid_size"]))
            for row in rows
        ] == [
            ("binance", datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc), pytest.approx(15), pytest.approx(100), 2.0),
            ("binance", datetime(2026, 11, 3, 10, 5, tzinfo=timezone.utc), pytest.approx(20), pytest.approx(200), 1.0),
        ]
    finally:
        store.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
