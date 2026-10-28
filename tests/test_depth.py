from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.depth import WINDOW_BPS, depth_snapshots
from core.execution import BookLevel, DepthBook
from web import create_app

D = Decimal
NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


def book(venue="a", *, bids, asks, age_seconds=0):
    return DepthBook(venue, "BTC", "USD", NOW - timedelta(seconds=age_seconds),
                     tuple(BookLevel(D(price), D(size)) for price, size in bids),
                     tuple(BookLevel(D(price), D(size)) for price, size in asks))


def capture(*books):
    return {"books": list(books), "fx": {"rates": {"USD": "1"}}}


def test_levels_land_in_one_bp_buckets_from_the_mid_and_reach_is_kept_beyond_the_window():
    # The mid is 100, so 1 bp is 0.01; the last bid sits 150 bps out, past the window.
    row, = depth_snapshots("BTC", capture(book(
        bids=(("99.995", "2"), ("99.99", "3"), ("99.985", "4"), ("98.5", "5")),
        asks=(("100.005", "1"), ("100.02", "6")),
    )), NOW)
    assert row.mid == 100 and len(row.bids) == len(row.asks) == WINDOW_BPS
    assert row.bids[:3] == (2.0, 7.0, 0.0) and sum(row.bids) == 9.0
    assert row.asks[:3] == (1.0, 0.0, 6.0)
    assert (row.bid_reach_bps, row.ask_reach_bps) == (150.0, 2.0)
    assert (row.bid_levels, row.ask_levels) == (4, 2)


def test_books_without_a_meaningful_mid_or_too_old_for_their_slot_are_skipped():
    # Thirty seconds is too old to quote from but still describes a five-minute slot.
    rows = depth_snapshots("BTC", capture(
        book("kept", bids=(("99", "1"),), asks=(("101", "1"),), age_seconds=30),
        book("one-sided", bids=(("99", "1"),), asks=()),
        book("crossed", bids=(("102", "1"),), asks=(("101", "1"),)),
        book("old", bids=(("99", "1"),), asks=(("101", "1"),), age_seconds=61),
    ), NOW)
    assert [row.venue for row in rows] == ["kept"]
    assert rows[0].book_age_seconds == 30


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OVERSEER_MOCK", "1")
    monkeypatch.setenv("FLASK_SECRET_KEY", "depth-test")
    monkeypatch.delenv("OVERSEER_PROXY_HOPS", raising=False)
    monkeypatch.delenv("OVERSEER_SECURE_COOKIES", raising=False)
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


@pytest.mark.parametrize("query", [
    "asset=DOGE&venue=binance_perp&hours=24",
    "asset=BTC&venue=coinbase&hours=24",
    "asset=BTC&venue=binance_perp&hours=12",
])
def test_heatmap_api_rejects_unsupported_requests(client, query):
    assert client.get(f"/api/depth-heatmap?{query}").status_code == 400


def test_a_week_of_depth_is_one_snapshot_per_half_hour(client):
    body = client.get("/api/depth-heatmap?asset=BTC&venue=binance_perp&hours=168").get_json()
    times = [column["time"] for column in body["columns"]]
    assert body["step_seconds"] == 1800 and len(times) > 1
    assert {later - earlier for earlier, later in zip(times, times[1:])} == {1800}
