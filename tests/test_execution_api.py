from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time

from aiosonic.exceptions import BaseTimeout
import pytest

from core import execution_fees
from core.execution import BookLevel, DepthBook
from data_collection.http import HttpError
from web import create_app
from data_collection.execution_reader import ExecutionReader

D = Decimal


def schedule(base, vip):
    return {"label": "Test venue", "default_tier": "base", "source_url": "https://example.invalid/fees",
            "checked_at": "2026-10-12", "notes": [], "tiers": [
                {"id": "base", "label": "Base", "taker_bps": base},
                {"id": "vip_1", "label": "VIP 1", "taker_bps": vip},
            ]}


class SnapshotReader:
    ask = D("100")
    add_unknown = False

    def capture(self, listings):
        now = datetime.now(timezone.utc)
        def depth(venue, bid, ask, currency="USD"):
            return DepthBook(venue, listings[venue], currency, now,
                             (BookLevel(D(bid), D("2000000")),),
                             (BookLevel(D(ask), D("2000000")),))
        books = [depth("binance_perp", "99", self.ask),
                 depth("bybit_perp", "99.002", self.ask + D("0.01"))]
        if self.add_unknown:
            books.append(depth("rise", "99", "100", "USDC"))
        return {"books": books, "errors": {}, "fx": {"rates": {"USD": "1", "USDC": "0.9"},
                "ts": now.isoformat(), "source": "Controlled FX", "source_url": "https://example.invalid/fx", "error": None}}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("OVERSEER_MOCK", "1")
    monkeypatch.setenv("FLASK_SECRET_KEY", "execution-test")
    monkeypatch.delenv("OVERSEER_PROXY_HOPS", raising=False)
    monkeypatch.delenv("OVERSEER_SECURE_COOKIES", raising=False)
    monkeypatch.setattr(execution_fees, "FEE_SCHEDULES", {
        "binance_perp": schedule("5", "1"), "bybit_perp": schedule("5.5", "3"),
    })
    app = create_app()
    app.config["TESTING"] = True
    real_reader = app.extensions["execution_reader"]
    reader = SnapshotReader()
    app.extensions["execution_reader"] = reader
    yield app.test_client(), reader
    real_reader.close()
    app.extensions["position_reader"].close()


def rows(payload):
    return {row["venue"]: row for row in payload["venues"]}


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_native_vip_selection_changes_actual_fee_adjusted_winner(setup, side):
    client, _ = setup
    base = client.post("/api/execution", json={"asset": "BTC", "side": side}).get_json()
    vip = client.post("/api/execution", json={"asset": "BTC", "side": side, "fees": {"bybit_perp": "vip_1"}}).get_json()
    assert rows(base)["binance_perp"]["estimates"][0]["best"]
    assert not rows(base)["bybit_perp"]["estimates"][0]["best"]
    assert rows(vip)["bybit_perp"]["estimates"][0]["best"]
    assert not rows(vip)["binance_perp"]["estimates"][0]["best"]
    gross = D("100.01") if side == "buy" else D("99.002")
    factor = D("1.0003") if side == "buy" else D("0.9997")
    cell = rows(vip)["bybit_perp"]["estimates"][0]
    assert abs(D(cell["effective_price_usd"]) - gross * factor) < D("1e-30")
    assert D(cell["top_of_book_price"]) == gross
    assert D(cell["top_of_book_price_usd"]) == gross
    expected_fee = D("0.030003") if side == "buy" else D("0.0297006")
    assert D(cell["fee_per_coin"]) == expected_fee
    assert D(cell["fee_per_coin_usd"]) == expected_fee
    assert abs(D(cell["fee_usd"]) - expected_fee * D(cell["quantity"])) < D("1e-24")
    assert D(cell["fee_usd"]) > expected_fee


def test_manual_refresh_returns_new_book_price_without_response_cache(setup):
    client, reader = setup
    first = client.post("/api/execution", json={"asset": "BTC", "side": "buy"})
    reader.ask = D("102")
    refreshed = client.post("/api/execution", json={"asset": "BTC", "side": "buy"})
    before = rows(first.get_json())["binance_perp"]["estimates"][0]
    after = rows(refreshed.get_json())["binance_perp"]["estimates"][0]
    assert D(before["effective_price_usd"]) == D("100.05")
    assert D(after["effective_price_usd"]) == D("102.051")
    assert D(before["top_of_book_price"]) == 100 and D(before["top_of_book_price_usd"]) == 100
    assert D(after["top_of_book_price"]) == 102 and D(after["top_of_book_price_usd"]) == 102
    assert D(before["fee_per_coin"]) == D("0.05") and D(before["fee_per_coin_usd"]) == D("0.05")
    assert D(after["fee_per_coin"]) == D("0.051") and D(after["fee_per_coin_usd"]) == D("0.051")
    assert "no-store" in refreshed.headers["Cache-Control"]
    assert "X-Overseer-Cache" not in refreshed.headers


def test_unverified_public_fee_remains_gross_only_not_free_or_best(setup, monkeypatch):
    client, reader = setup
    reader.add_unknown = True
    monkeypatch.setitem(execution_fees.FEE_SCHEDULES, "rise", schedule(None, None))
    payload = client.post("/api/execution", json={"asset": "BTC", "side": "buy"}).get_json()
    cell = rows(payload)["rise"]["estimates"][0]
    assert cell["status"] == "fee_unknown" and cell["complete"]
    assert D(cell["average_price_usd"]) == 90
    assert cell["effective_price_usd"] is None and cell["fee_usd"] is None and not cell["best"]
    assert D(cell["top_of_book_price"]) == 100 and D(cell["top_of_book_price_usd"]) == 90
    assert cell["fee_per_coin"] is None and cell["fee_per_coin_usd"] is None


def test_round_trip_ranks_the_whole_trade_and_follows_fee_tiers(setup):
    client, _ = setup
    base = client.post("/api/execution", json={"asset": "BTC", "side": "buy_sell"}).get_json()
    assert (base["side"], base["hold_hours"]) == ("buy_sell", 0)
    cell = rows(base)["binance_perp"]["estimates"][0]
    # Bought at 100 plus 5 bps, sold at 99 less 5 bps.
    assert abs(D(cell["total_cost_usd"]) - D(cell["quantity"]) * (D("100.05") - D("98.9505"))) < D("1e-24")
    assert cell["best"] and not rows(base)["bybit_perp"]["estimates"][0]["best"]
    vip = client.post("/api/execution", json={
        "asset": "BTC", "side": "sell_buy", "fees": {"bybit_perp": "vip_1"}}).get_json()
    assert rows(vip)["bybit_perp"]["estimates"][0]["best"]


@pytest.mark.parametrize("hold,method,basis,steps", [
    (24, "current_funding_rates", "current", 1),
    (168, "settled_funding_rates", "previous_24h", 3),
])
def test_held_trades_rank_after_funding_and_unreadable_funding_ranks_nothing(setup, monkeypatch, hold, method, basis, steps):
    client, _ = setup
    store = client.application.extensions["read_storage"]
    now = datetime.now(timezone.utc)
    # Binance longs pay 0.04% every 8 hours; Bybit's funding is flat.
    rates = {"binance": D("0.0004"), "bybit": D("0")}
    monkeypatch.setattr(store, method, lambda exchanges, symbols, *since: [
        {"exchange": exchange, "symbol": "BTC/USDT", "ts": now - timedelta(hours=8 * step),
         "rate": rate, "interval_hours": 8}
        for exchange, rate in rates.items() for step in range(steps)])
    long = client.post("/api/execution", json={"asset": "BTC", "side": "buy", "hold_hours": hold}).get_json()
    binance, bybit = rows(long)["binance_perp"], rows(long)["bybit_perp"]
    assert binance["funding"]["basis"] == basis
    assert D(binance["funding"]["cost_bps"]) == D("0.0004") / 8 * hold * 10000
    assert bybit["estimates"][0]["best"] and not binance["estimates"][0]["best"]
    short = client.post("/api/execution", json={"asset": "BTC", "side": "sell", "hold_hours": hold}).get_json()
    assert D(rows(short)["binance_perp"]["funding"]["cost_bps"]) == -D(binance["funding"]["cost_bps"])

    def unreadable(*_args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, method, unreadable)
    failed = client.post("/api/execution", json={"asset": "BTC", "side": "buy", "hold_hours": hold}).get_json()
    for row in failed["venues"]:
        assert row["status"] == "funding_unknown"
        assert row["funding"]["unavailable"] == "Funding data could not be read"
        assert all(not cell["best"] and cell["effective_price_usd"] is not None for cell in row["estimates"])


@pytest.mark.parametrize("body", [
    {"asset": "NOT_CONFIGURED", "side": "buy"},
    {"asset": "BTC", "side": "long"},
    {"asset": "BTC", "side": ["buy"]},
    {"asset": "BTC", "side": "buy", "fees": []},
    {"asset": "BTC", "side": "buy", "fees": {"binance_perp": "invented_tier"}},
    {"asset": "BTC", "side": "buy", "fees": {"unsupported": "base"}},
    {"asset": "BTC", "side": "buy", "account": "not an account-reading endpoint"},
    {"asset": "BTC", "side": "buy", "hold_hours": 12},
    {"asset": "BTC", "side": "buy", "hold_hours": True},
    {"asset": "BTC", "side": "buy", "hold_hours": "24"},
])
def test_unknown_coin_side_and_fee_assumptions_are_rejected(setup, body):
    client, _ = setup
    response = client.post("/api/execution", json=body)
    assert response.status_code == 400 and "no-store" in response.headers["Cache-Control"]


@pytest.fixture
def public_reader(setup):
    client, _ = setup
    readers = []

    def install(fetch, *, fx_error=None):
        class Provider:
            async def fetch(self, venue, symbol):
                return await fetch(venue, symbol)

        class ReferenceFX:
            async def get_json(self, url, **kwargs):
                if fx_error is not None:
                    raise fx_error
                return {"data": {"currency": "USD", "rates": {"USDC": "1", "USDT": "1"}}}

            async def aclose(self):
                pass

        class OfflineReader(ExecutionReader):
            async def _ensure_clients(self):
                if self._provider is None:
                    self._provider = Provider()
                    self._parallel = asyncio.Semaphore(self.parallelism)
                    self._fx_lock = asyncio.Lock()
                    self._clients = {"fx": ReferenceFX()}

        reader = OfflineReader()
        # Keep production budget proportions but avoid multi-second suite waits.
        reader.individual_timeout *= 0.05
        reader.deadline *= 0.05
        readers.append(reader)
        client.application.extensions["execution_reader"] = reader
        return reader

    yield client, install
    for reader in readers:
        reader.close()


def public_book(venue, symbol, currency="USD"):
    return DepthBook(venue, symbol, currency, datetime.now(timezone.utc),
                     (BookLevel(D("99"), D("2000000")),),
                     (BookLevel(D("100"), D("2000000")),))


def test_slower_public_provider_succeeds_without_retry_or_losing_healthy_sibling(public_reader):
    client, install = public_reader
    calls = {}

    async def fetch(venue, symbol):
        calls[venue] = calls.get(venue, 0) + 1
        if venue == "bybit_perp":
            # Six seconds scaled down: beyond the former five-second budget.
            await asyncio.sleep(0.3)
        return public_book(venue, symbol)

    install(fetch)
    response = client.post("/api/execution", json={"asset": "BTC", "side": "buy"})
    assert response.status_code == 200
    result = rows(response.get_json())
    for venue in ("binance_perp", "bybit_perp"):
        assert result[venue]["error"] is None
        assert result[venue]["estimates"][0]["complete"]
        assert D(result[venue]["estimates"][0]["effective_price_usd"]) > 0
    assert all(count == 1 for count in calls.values())


@pytest.mark.parametrize("limit", ["individual", "overall"])
def test_hanging_provider_preserves_healthy_rows_and_finishes_cancellation(public_reader, limit):
    client, install = public_reader
    cleaned = []
    calls = {}

    async def fetch(venue, symbol):
        calls[venue] = calls.get(venue, 0) + 1
        if venue == "bybit_perp":
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                cleaned.append(venue)
        return public_book(venue, symbol)

    reader = install(fetch)
    if limit == "individual":
        reader.individual_timeout = 0.03
        reader.deadline = 0.5
    else:
        reader.individual_timeout = 0.5
        reader.deadline = 0.03
    started = time.monotonic()
    response = client.post("/api/execution", json={"asset": "BTC", "side": "buy"})
    assert time.monotonic() - started < 1
    assert response.status_code == 200
    result = rows(response.get_json())
    assert result["binance_perp"]["estimates"][0]["complete"]
    failed = result["bybit_perp"]
    assert failed["status"] == "unavailable"
    assert all(not cell["best"] and cell["effective_price_usd"] is None for cell in failed["estimates"])
    for cell in failed["estimates"]:
        assert cell["top_of_book_price"] is None and cell["top_of_book_price_usd"] is None
        assert cell["fee_per_coin"] is None and cell["fee_per_coin_usd"] is None
    assert cleaned == ["bybit_perp"]
    assert all(count == 1 for count in calls.values())
    # A completed timeout must not retain admission or worker slots.
    again = client.post("/api/execution", json={"asset": "BTC", "side": "buy"})
    assert again.status_code == 200
    assert rows(again.get_json())["binance_perp"]["estimates"][0]["complete"]
    assert cleaned == ["bybit_perp", "bybit_perp"]


@pytest.mark.parametrize("status, cause", [
    (503, None),
    (429, None),
    (None, BaseTimeout),
    (None, ConnectionError),
])
def test_upstream_failures_are_safe_and_do_not_hide_healthy_rows(public_reader, status, cause):
    client, install = public_reader
    secret = "https://private.invalid/depth?token=credential"
    calls = {}

    async def fetch(venue, symbol):
        calls[venue] = calls.get(venue, 0) + 1
        if venue == "bybit_perp":
            error = HttpError(status, secret, body="private upstream payload")
            if cause is not None:
                raise error from cause(secret)
            raise error
        return public_book(venue, symbol)

    install(fetch)
    response = client.post("/api/execution", json={"asset": "BTC", "side": "buy"})
    assert response.status_code == 200
    result = rows(response.get_json())
    assert result["binance_perp"]["estimates"][0]["complete"]
    if status is not None:
        assert str(status) in result["bybit_perp"]["error"]
    assert result["bybit_perp"]["status"] == "unavailable"
    assert all(not cell["best"] and cell["effective_price_usd"] is None
               for cell in result["bybit_perp"]["estimates"])
    for cell in result["bybit_perp"]["estimates"]:
        assert cell["top_of_book_price"] is None and cell["top_of_book_price_usd"] is None
        assert cell["fee_per_coin"] is None and cell["fee_per_coin_usd"] is None
    assert "private.invalid" not in response.get_data(as_text=True)
    assert "private upstream payload" not in response.get_data(as_text=True)
    assert all(count == 1 for count in calls.values())


@pytest.mark.parametrize("status, cause", [
    (502, None),
    (None, BaseTimeout),
])
def test_reference_fx_failures_are_safe_without_inventing_rates(public_reader, status, cause):
    client, install = public_reader
    error = HttpError(status, "private transport message", body="private upstream payload")
    if cause is not None:
        error.__cause__ = cause("private transport message")

    async def fetch(venue, symbol):
        return public_book(venue, symbol, "USDC")

    install(fetch, fx_error=error)
    response = client.post("/api/execution", json={"asset": "BTC", "side": "buy"})
    assert response.status_code == 200
    payload = response.get_json()
    if status is not None:
        assert str(status) in payload["fx"]["error"]
    assert payload["fx"]["ts"] is None
    assert payload["fx"]["rates"] == {"USD": "1"}
    assert all(not cell["best"] and cell["effective_price_usd"] is None
               for row in payload["venues"] for cell in row["estimates"])
    for row in payload["venues"]:
        for cell in row["estimates"]:
            assert cell["top_of_book_price"] is None and cell["top_of_book_price_usd"] is None
            assert cell["fee_per_coin"] is None and cell["fee_per_coin_usd"] is None
    assert "private transport message" not in response.get_data(as_text=True)
    assert "private upstream payload" not in response.get_data(as_text=True)
