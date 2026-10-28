from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core import execution_fees
from core.execution import BookLevel, DepthBook
from core.execution_history import HISTORY_NOTIONALS, history_samples, summarise
from scheduler.jobs import capture_execution_history
from web import create_app

D = Decimal
NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def row(venue, price, *, status="ok", notional=10_000, ts=NOW):
    return {"ts": ts, "notional_usd": notional, "venue": venue, "status": status,
            "average_price_usd": price if status == "ok" else None}


def size(summary, notional=10_000):
    return next(item for item in summary if item["notional_usd"] == str(notional))


def venue(item, name):
    return next(entry for entry in item["venues"] if entry["venue"] == name)


@pytest.mark.parametrize("side,cheap_pre_fee,fair", [
    # "a" has the better pre-fee fill, but its 5 bps fee makes "b" cheaper.
    ("buy", 100.00, 100.03),
    ("sell", 100.00, 99.98),
])
def test_fees_are_applied_at_read_time_and_rank_by_side(side, cheap_pre_fee, fair):
    rows = [row("a", cheap_pre_fee), row("b", fair)]
    base = size(summarise(rows, side, {"a": 5.0, "b": 0.0}))
    assert base["ranked"] == 1
    assert venue(base, "b")["win_share_pct"] == 100.0
    assert venue(base, "a")["win_share_pct"] == 0.0
    effective = cheap_pre_fee * (1.0005 if side == "buy" else 0.9995)
    expected_gap = (effective / fair - 1 if side == "buy" else 1 - effective / fair) * 10000
    assert venue(base, "a")["median_gap_bps"] == pytest.approx(round(expected_gap, 2))
    # The same stored samples rank differently under another fee tier.
    vip = size(summarise(rows, side, {"a": 0.0, "b": 0.0}))
    assert venue(vip, "a")["win_share_pct"] == 100.0


def test_only_complete_quotes_with_known_fees_are_compared():
    first, second = NOW, NOW + timedelta(minutes=5)
    rows = [
        # One rankable quote is not a comparison.
        row("a", 100.0, ts=first), row("b", None, status="stale", ts=first),
        row("c", 99.0, ts=first),
        row("a", 100.0, ts=second), row("b", 100.2, ts=second), row("c", 99.0, ts=second),
    ]
    item = size(summarise(rows, "buy", {"a": 0.0, "b": 0.0, "c": None}))
    assert (item["captures"], item["ranked"]) == (2, 1)
    assert venue(item, "a")["win_share_pct"] == 100.0
    stale = venue(item, "b")
    assert stale["quoted_pct"] == 50.0 and stale["excluded"] == {"stale": 1}
    # An unknown fee never wins, even with the best pre-fee fill, and sorts last.
    unknown = venue(item, "c")
    assert unknown["win_share_pct"] is None and unknown["quoted_pct"] == 100.0
    assert item["venues"][-1]["venue"] == "c"


def test_ties_split_the_win():
    item = size(summarise([row("a", 100.0), row("b", 100.0), row("c", 100.5)], "buy",
                          {"a": 0.0, "b": 0.0, "c": 0.0}))
    assert venue(item, "a")["win_share_pct"] == venue(item, "b")["win_share_pct"] == 50.0
    assert venue(item, "c")["win_share_pct"] == 0.0


def legs(venue_id, buy, sell, *, sell_status="ok"):
    return [{**row(venue_id, buy), "side": "buy"},
            {**row(venue_id, sell, status=sell_status), "side": "sell"}]


def test_round_trips_pair_each_venues_legs_from_one_capture():
    # a buys cheapest, but b's tighter book is cheaper to get in and out of.
    rows = legs("a", 100.0, 99.0) + legs("b", 100.5, 100.0) + legs("c", 100.2, None, sell_status="stale")
    fees = {"a": 5.0, "b": 5.0, "c": 5.0}
    assert venue(size(summarise(rows, "buy", fees)), "a")["win_share_pct"] == 100.0
    item = size(summarise(rows, "round_trip", fees))
    assert (item["captures"], item["ranked"]) == (1, 1)
    assert venue(item, "b")["win_share_pct"] == 100.0
    # a's round trip costs 1.0995 per coin against b's 0.60025: about 49.8 bps of the price.
    assert venue(item, "a")["median_gap_bps"] == pytest.approx(49.8, abs=0.01)
    stale = venue(item, "c")
    assert stale["excluded"] == {"stale": 1} and stale["quoted_pct"] == 0.0 and stale["samples"] == 1


def depth(venue_id, *, age_seconds=0):
    return DepthBook(venue_id, "BTC", "USD", NOW - timedelta(seconds=age_seconds),
                     (BookLevel(D("99"), D("1000000")),), (BookLevel(D("101"), D("1000000")),))


def test_samples_keep_pre_fee_fills_and_record_why_venues_were_not_comparable():
    snapshot = {"books": [depth("a"), depth("b", age_seconds=30)], "errors": {"c": "timeout"},
                "fx": {"rates": {"USD": "1"}}}
    samples = history_samples("BTC", {"a": "BTC", "b": "BTC", "c": "BTC"}, snapshot, NOW)
    assert {sample.notional_usd for sample in samples} == set(HISTORY_NOTIONALS)
    assert len(samples) == 3 * 2 * len(HISTORY_NOTIONALS)
    fresh = [sample for sample in samples if sample.venue == "a"]
    assert all(sample.status == "ok" for sample in fresh)
    assert {sample.side: sample.average_price_usd for sample in fresh} == {"buy": D("101"), "sell": D("99")}
    stale = [sample for sample in samples if sample.venue == "b"]
    assert {sample.status for sample in stale} == {"stale"}
    assert all(sample.average_price_usd is None and sample.book_age_seconds == 30 for sample in stale)
    missing = [sample for sample in samples if sample.venue == "c"]
    assert {sample.status for sample in missing} == {"unavailable"}
    assert all(sample.book_age_seconds is None for sample in missing)


class Reader:
    def __init__(self, result):
        self.result = result

    def capture(self, listings):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Storage:
    def __init__(self):
        self.samples = []
        self.depth = []

    async def write_execution_samples(self, samples):
        self.samples.extend(samples)
        return len(samples)

    async def write_depth_snapshots(self, rows):
        self.depth.extend(rows)
        return len(rows)


def live_book(venue_id):
    return DepthBook(venue_id, "BTC", "USD", datetime.now(timezone.utc),
                     (BookLevel(D("99"), D("1000000")),), (BookLevel(D("101"), D("1000000")),))


class Registry:
    def listings(self, asset):
        return {"binance_perp": "BTC/USDT", "bybit_perp": "BTC/USDT"}


@pytest.mark.asyncio
@pytest.mark.parametrize("result,status,samples,depth_rows", [
    ({"books": [live_book("binance_perp"), live_book("bybit_perp")], "errors": {}, "fx": {"rates": {"USD": "1"}}},
     "ok", 12, 2),
    # Every venue failing at once is more likely our network than the venues.
    ({"books": [], "errors": {"binance_perp": "x", "bybit_perp": "x"}, "fx": {"rates": {"USD": "1"}}}, "fail", 0, 0),
    (RuntimeError("reader down"), "fail", 0, 0),
])
async def test_capture_job_records_samples_and_depth_only_when_some_venue_answered(result, status, samples, depth_rows):
    storage = Storage()
    outcome = await capture_execution_history(Reader(result), storage, Registry(), "BTC")
    assert outcome.job_id == "execution:BTC"
    assert outcome.status == status
    assert (len(storage.samples), len(storage.depth)) == (samples, depth_rows)
    assert outcome.new_rows == samples + depth_rows


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OVERSEER_MOCK", "1")
    monkeypatch.setenv("FLASK_SECRET_KEY", "history-test")
    monkeypatch.delenv("OVERSEER_PROXY_HOPS", raising=False)
    monkeypatch.delenv("OVERSEER_SECURE_COOKIES", raising=False)
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


@pytest.mark.parametrize("query", [
    "asset=DOGE&side=buy&days=7",
    "asset=BTC&side=hold&days=7",
    "asset=BTC&side=buy&days=5",
    "asset=BTC&side=buy&days=7&fees=unknown_venue:vip_1",
    "asset=BTC&side=buy&days=7&fees=binance_perp:vip_99",
])
def test_history_api_rejects_unsupported_requests(client, query):
    assert client.get(f"/api/execution-history?{query}").status_code == 400


def test_history_api_reports_tracked_sizes_and_never_ranks_unknown_fees(client, monkeypatch):
    # A venue whose fee is unknown is listed but never ranked.
    monkeypatch.setitem(execution_fees.FEE_SCHEDULES, "rise", {
        **execution_fees.FEE_SCHEDULES["rise"], "default_tier": "unknown",
        "tiers": [{"id": "unknown", "label": "Unknown", "taker_bps": None}],
    })
    body = client.get("/api/execution-history?asset=BTC&side=buy&days=7").get_json()
    assert [item["notional_usd"] for item in body["sizes"]] == [str(int(value)) for value in HISTORY_NOTIONALS]
    for item in body["sizes"]:
        shares = [entry["win_share_pct"] for entry in item["venues"] if entry["win_share_pct"] is not None]
        assert sum(shares) == pytest.approx(100, abs=0.5)
        assert venue(item, "rise")["win_share_pct"] is None
    custom = client.get("/api/execution-history?asset=BTC&side=buy&days=7&fees=binance_perp:vip_9").get_json()
    assert custom["custom_fees"] and not body["custom_fees"]


def test_history_api_ranks_round_trips_from_both_sides(client):
    body = client.get("/api/execution-history?asset=BTC&side=round_trip&days=7").get_json()
    assert body["side"] == "round_trip"
    assert all(item["ranked"] > 0 for item in body["sizes"])
