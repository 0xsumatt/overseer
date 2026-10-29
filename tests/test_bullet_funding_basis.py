from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest


@pytest.mark.asyncio
async def test_bullet_funding_migration_rescales_eight_hour_rows_once():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for the PostgreSQL migration regression")
    schema = f"bullet_basis_{uuid4().hex}"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        await connection.execute(f"SET search_path TO {schema}")
        # The columns 003 and 008 create, as plain tables: hypertables need TimescaleDB.
        await connection.execute(
            """
            CREATE TABLE funding_rates (exchange text, symbol text, ts timestamptz,
                                        rate numeric, interval_hours int);
            CREATE TABLE liquidity (exchange text, symbol text, ts timestamptz,
                                    current_funding_rate numeric, funding_interval_hours int);
            INSERT INTO funding_rates VALUES
                ('bullet',       'BTC-USD', '2026-09-01 00:00+00', 0.0001,  1),
                ('bullet',       'SOL-USD', '2026-09-01 00:00+00', -0.0004, 1),
                ('binance_perp', 'BTCUSDT', '2026-09-01 00:00+00', 0.0001,  8);
            -- Five-minute snapshots: Bullet switched basis between 10:00 and 10:05.
            INSERT INTO liquidity VALUES
                ('bullet',       'ETH-USD', '2026-10-27 10:00+00', 0.0000125, 1),
                ('bullet',       'SOL-USD', '2026-10-27 10:00+00', 0.00002,   1),
                ('bullet',       'ETH-USD', '2026-10-27 10:05+00', 0.0001,    1),
                ('bullet',       'SOL-USD', '2026-10-27 10:05+00', 0.00016,   1),
                ('binance_perp', 'BTCUSDT', '2026-10-27 10:05+00', 0.0001,    8);
            """
        )
        migrations = Path(__file__).resolve().parents[1] / "migrations"
        migration = (migrations / "014_bullet_funding_basis.sql").read_text()

        await connection.execute(migration)
        # The fixed scheduler then stores per-settlement rates, which a second
        # application must leave alone, as it must the rows already rescaled.
        await connection.execute(
            """
            INSERT INTO funding_rates VALUES ('bullet', 'BTC-USD', '2026-10-29 00:00+00', 0.0000125, 1);
            INSERT INTO liquidity VALUES ('bullet', 'ETH-USD', '2026-10-29 00:00+00', 0.0000125, 1);
            """
        )
        await connection.execute(migration)

        settled = await connection.fetch(
            "SELECT exchange, symbol, rate FROM funding_rates ORDER BY ts, exchange, symbol"
        )
        assert [tuple(row) for row in settled] == [
            ("binance_perp", "BTCUSDT", Decimal("0.0001")),
            ("bullet", "BTC-USD", Decimal("0.0000125")),
            ("bullet", "SOL-USD", Decimal("-0.00005")),
            ("bullet", "BTC-USD", Decimal("0.0000125")),
        ]
        current = await connection.fetch(
            "SELECT exchange, symbol, current_funding_rate FROM liquidity "
            "ORDER BY ts, exchange, symbol"
        )
        assert [tuple(row) for row in current] == [
            # Before the switch Bullet's current rate was already hourly.
            ("bullet", "ETH-USD", Decimal("0.0000125")),
            ("bullet", "SOL-USD", Decimal("0.00002")),
            ("binance_perp", "BTCUSDT", Decimal("0.0001")),
            ("bullet", "ETH-USD", Decimal("0.0000125")),
            ("bullet", "SOL-USD", Decimal("0.00002")),
            ("bullet", "ETH-USD", Decimal("0.0000125")),
        ]
    finally:
        await connection.execute("RESET search_path")
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()
