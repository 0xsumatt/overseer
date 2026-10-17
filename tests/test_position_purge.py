from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest


@pytest.mark.asyncio
async def test_positions_cutover_purges_linked_data_without_deleting_logins_or_market_data():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for the PostgreSQL purge regression")
    schema = f"position_purge_{uuid4().hex}"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        await connection.execute(f"SET search_path TO {schema}")
        migrations = Path(__file__).resolve().parents[1] / "migrations"
        await connection.execute((migrations / "004_users.sql").read_text())
        await connection.execute((migrations / "010_position_tracker.sql").read_text())
        user = await connection.fetchval(
            "INSERT INTO users (email, password_hash) VALUES ('keep@test', 'hash') RETURNING id"
        )
        saved = await connection.fetchval(
            "INSERT INTO position_accounts (user_id, venue, address, label, token_ciphertext, snapshot) "
            "VALUES ($1, 'lighter', '123', 'remove', 'encrypted-secret', '{\"positions\": []}') RETURNING id",
            user,
        )
        await connection.execute(
            "INSERT INTO position_funding_events (account_id, event_id, ts, symbol, amount, currency) "
            "VALUES ($1, 'remove-payment', now(), 'BTC', 1, 'USDC')",
            saved,
        )
        await connection.execute(
            "INSERT INTO position_fx VALUES (TRUE, '{\"USDC\": \"0.99\"}', now(), 'old-public-fx')"
        )
        await connection.execute("CREATE TABLE market_data (symbol text, price numeric)")
        await connection.execute("INSERT INTO market_data VALUES ('BTC', 100)")

        purge = (migrations / "011_browser_positions.sql").read_text()
        await connection.execute(purge)
        assert await connection.fetchval("SELECT email FROM users WHERE id = $1", user) == "keep@test"
        assert await connection.fetchval("SELECT price FROM market_data WHERE symbol = 'BTC'") == 100
        for table in ("position_accounts", "position_funding_events", "position_fx"):
            assert await connection.fetchval("SELECT to_regclass($1)", f"{schema}.{table}") is None
        # Existing and fresh installs can apply the cutover without resurrecting data.
        await connection.execute(purge)
        assert await connection.fetchval("SELECT count(*) FROM users") == 1
        assert await connection.fetchval("SELECT count(*) FROM market_data") == 1
    finally:
        await connection.execute("RESET search_path")
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()
