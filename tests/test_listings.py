from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from core.models import Listing
from scheduler.listings import Findings, Seen, check, posts
from storage.writers import Storage

MONDAY = datetime(2026, 11, 30, 6, 20, tzinfo=timezone.utc)
EXCHANGES = {"bybit_perp": "Bybit", "bulk": "BULK", "qfex": "QFEX", "lighter": "Lighter",
             "extended": "Extended"}
CONFIGURED = {
    "bybit_perp": {"XAU/USDT": "XAU", "AAPL/USDT": "AAPL"},
    "qfex": {"GOLD-USD": "XAU"},
    "bulk": {"NVDA-USD": "NVDA"},
}
MARKETS = {
    "bybit_perp": [Listing("XAU/USDT", "XAU"), Listing("AAPL/USDT", "AAPL")],
    "qfex": [Listing("GOLD-USD", "GOLD")],
    "bulk": [Listing("NVDA-USD", "NVDA"), Listing("AAPL-USD", "AAPL"),
             Listing("GOLD-USD", "GOLD"), Listing("PUMP-USD", "PUMP")],
}


def _after(seen: dict[tuple[str, str], Seen], found: Findings) -> dict[tuple[str, str], Seen]:
    """The state the check leaves: its rows written over the previous ones."""
    out = dict(seen)
    for venue, symbol, base, first_seen, last_seen, listed in found.rows:
        before = out.get((venue, symbol))
        out[(venue, symbol)] = Seen(base, before.first_seen if before else first_seen,
                                    last_seen, listed)
    return out


def test_tracked_assets_newly_listed_are_reported_once_under_any_name_they_go_by() -> None:
    found = check(CONFIGURED, MARKETS, {}, EXCHANGES, MONDAY, weekly=False)
    # QFEX calls gold GOLD, so BULK's GOLD-USD is gold too; PUMP is not tracked.
    assert found.new == [("bulk", "AAPL", "AAPL-USD"), ("bulk", "XAU", "GOLD-USD")]
    assert ('• **BULK** lists AAPL as `AAPL-USD`: add `bulk = "AAPL-USD"` under '
            '`[assets.AAPL]`') in posts(found, {v: v.upper() if v == "bulk" else v for v in EXCHANGES})[0]

    again = check(CONFIGURED, MARKETS, _after({}, found), EXCHANGES, MONDAY + timedelta(days=1),
                  weekly=False)
    assert (again.new, again.gone, again.back) == ([], [], [])


def test_a_configured_market_that_disappears_is_reported_once_and_again_when_back() -> None:
    seen = _after({}, check(CONFIGURED, MARKETS, {}, EXCHANGES, MONDAY, weekly=False))
    without_gold = dict(MARKETS, qfex=[])

    gone = check(CONFIGURED, without_gold, seen, EXCHANGES, MONDAY, weekly=False)
    assert gone.gone == [("qfex", "XAU", "GOLD-USD")]
    seen = _after(seen, gone)
    # Still missing: nothing new to say, and gold keeps its GOLD name meanwhile.
    still = check(CONFIGURED, without_gold, seen, EXCHANGES, MONDAY, weekly=False)
    assert (still.gone, still.new) == ([], [])

    back = check(CONFIGURED, MARKETS, seen, EXCHANGES, MONDAY, weekly=False)
    assert (back.back, back.new) == ([("qfex", "XAU", "GOLD-USD")], [])


def test_weekly_spreading_counts_exchanges_but_not_what_was_listed_before_checks_began() -> None:
    # BULK's first check is the baseline: what it listed then is not "new".
    seen = _after({}, check({}, {"bulk": [Listing("PUMP-USD", "PUMP"),
                                          Listing("OLD-USD", "OLD")]},
                            {}, EXCHANGES, MONDAY - timedelta(days=30), weekly=False))
    for venue in ("bybit_perp", "lighter", "extended"):
        seen[(venue, "BTC")] = Seen("BTC", None, MONDAY - timedelta(days=30), True)
    markets = {
        "bulk": [Listing("PUMP-USD", "PUMP"), Listing("OLD-USD", "OLD")],
        "bybit_perp": [Listing("PUMP/USDT", "PUMP"), Listing("OLD/USDT", "OLD")],
        "lighter": [Listing("PUMP", "PUMP"), Listing("PAIR", "PAIR")],
        "extended": [Listing("PUMP-USD", "PUMP"), Listing("PAIR-USD", "PAIR")],
    }
    found = check({}, markets, seen, EXCHANGES, MONDAY, weekly=True)
    # OLD is on two exchanges, PAIR on two: only PUMP is spreading.
    assert found.spreading == [("PUMP", ["BULK", "Bybit", "Extended", "Lighter"],
                                ["Bybit", "Extended", "Lighter"])]
    assert check({}, markets, seen, EXCHANGES, MONDAY, weekly=False).spreading == []


@pytest.mark.asyncio
async def test_listings_round_trip_and_keep_when_first_seen() -> None:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for the PostgreSQL listings regression")
    schema = f"venue_listings_{uuid4().hex}"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        await connection.execute(f"SET search_path TO {schema}")
        migrations = Path(__file__).resolve().parents[1] / "migrations"
        await connection.execute((migrations / "016_venue_listings.sql").read_text())
        storage = Storage(dsn)
        storage._pool = connection  # type: ignore[assignment]

        await storage.write_venue_listings([
            ("bulk", "AAPL-USD", "AAPL", MONDAY, MONDAY, True),
            ("qfex", "GOLD-USD", "XAU", None, None, False),
        ])
        later = MONDAY + timedelta(days=1)
        await storage.write_venue_listings([("bulk", "AAPL-USD", "AAPL", later, later, False)])

        rows = {(r["venue"], r["symbol"]): tuple(r) for r in await storage.venue_listings()}
        assert rows == {
            ("bulk", "AAPL-USD"): ("bulk", "AAPL-USD", "AAPL", MONDAY, later, False),
            ("qfex", "GOLD-USD"): ("qfex", "GOLD-USD", "XAU", None, None, False),
        }
    finally:
        await connection.execute("RESET search_path")
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()
