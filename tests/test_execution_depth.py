from __future__ import annotations

import asyncio
from concurrent.futures import Future
from datetime import datetime, timezone
from decimal import Decimal
import threading

import pytest

from core.execution import BookLevel, DepthBook
from data_collection.execution import (
    DecimalHttpClient, DepthUnavailable, ExecutionDepth, levels, optional_limit,
)
from data_collection.execution_reader import ExecutionReader, ReaderBusy, _Admission


class HTTP:
    def __init__(self, responses):
        self.responses = responses
        self.closed = False

    async def get_json(self, url, **kwargs):
        for marker, payload in self.responses.items():
            if marker in url:
                return payload
        raise AssertionError(f"Unexpected public request: {url}")

    async def post_json(self, url, **kwargs):
        return self.responses[kwargs["json"]["type"]]

    async def aclose(self):
        self.closed = True


def provider(http):
    return ExecutionDepth({venue: http for venue in (
        "binance_perp", "bybit_perp", "hyperliquid", "lighter", "extended", "rise", "bullet", "bulk",
    )})


def book(venue="bulk"):
    return DepthBook(venue, "BTC-USD", "USD", datetime.now(timezone.utc),
                     (BookLevel(Decimal("100"), Decimal("2")),),
                     (BookLevel(Decimal("101"), Decimal("3")),))


@pytest.mark.asyncio
async def test_raw_json_decimal_tokens_do_not_round_through_float():
    class Response:
        async def text(self):
            return '{"px":100.123456789012345678901234,"sz":0.000000000000000000001}'

    class RawClient(DecimalHttpClient):
        def __init__(self):
            pass

        async def request(self, method, url, **kwargs):
            assert kwargs["retry"] is False
            return Response()

    payload = await RawClient().get_json("https://example.invalid")
    assert payload["px"] == Decimal("100.123456789012345678901234")
    assert payload["sz"] == Decimal("0.000000000000000000001")


@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-1", "0", True, 1.1])
def test_invalid_financial_levels_are_rejected(invalid):
    with pytest.raises(DepthUnavailable):
        levels([[invalid, "1"]], 0, 1, bids=True)


def test_disabled_market_filter_is_not_zero_order_limit():
    assert optional_limit("0.00000000") is None
    assert optional_limit("12.5") == Decimal("12.5")
    with pytest.raises(DepthUnavailable):
        optional_limit(True)


@pytest.mark.asyncio
async def test_lighter_remaining_human_sizes_duplicate_prices_and_market_id():
    http = HTTP({
        "/orderBookDetails": {"code": 200, "order_book_details": [{
            "symbol": "BTC", "market_id": 19, "status": "active",
            "supported_size_decimals": 5, "quote_asset_id": 0, "multiplier": "1",
        }]},
        "/orderBookOrders": {"code": 200,
            "bids": [{"price": "99", "remaining_base_amount": "0.12345", "initial_base_amount": "900"},
                     {"price": "100", "remaining_base_amount": "1"},
                     {"price": "99", "remaining_base_amount": "0.00001"}],
            "asks": [{"price": "102", "remaining_base_amount": "2"},
                     {"price": "101", "remaining_base_amount": "3"}]},
    })
    depth = await provider(http).fetch("lighter", "BTC")
    assert depth.bids == (BookLevel(Decimal("100"), Decimal("1")),
                          BookLevel(Decimal("99"), Decimal("0.12346")))
    assert [level.price for level in depth.asks] == [Decimal("101"), Decimal("102")]
    assert depth.venue_at is None


@pytest.mark.asyncio
async def test_bulk_human_sizes_usd_metadata_and_nanosecond_source_time():
    http = HTTP({
        "/exchangeInfo": [{"symbol": "BTC-USD", "baseAsset": "BTC", "quoteAsset": "USD", "status": "TRADING", "sizeDecimals": 8}],
        "/l2book": {"symbol": "BTC-USD", "updateType": "snapshot", "timestamp": 1791807763567684400,
                    "levels": [[{"px": Decimal("83062.63"), "sz": Decimal("1.06671294")}],
                               [{"px": Decimal("83063.00"), "sz": Decimal("0.50000001")}]]},
    })
    depth = await provider(http).fetch("bulk", "BTC-USD")
    assert depth.quote_currency == "USD"
    assert depth.bids[0].quantity == Decimal("1.06671294")
    assert depth.venue_at == datetime.fromtimestamp(1791807763, timezone.utc).replace(microsecond=567684)


@pytest.mark.asyncio
async def test_bulk_rejects_wrong_book_identity_and_suspended_metadata():
    metadata = {"symbol": "BTC-USD", "baseAsset": "BTC", "quoteAsset": "USD", "status": "SUSPENDED"}
    http = HTTP({"/exchangeInfo": [metadata], "/l2book": {"symbol": "ETH-USD"}})
    with pytest.raises(DepthUnavailable, match="suspended"):
        await provider(http).fetch("bulk", "BTC-USD")
    metadata["status"] = "TRADING"
    with pytest.raises(DepthUnavailable, match="identity"):
        await provider(http).fetch("bulk", "BTC-USD")


def extended_http(*, rfq=False, status="ACTIVE"):
    return HTTP({
        "/orderbook": {"status": "OK", "data": {"market": "BTC-USD", "bid": [{"price": "99", "qty": "0.12345"}], "ask": [{"price": "101", "qty": "2"}]}},
        "/info/markets": {"status": "OK", "data": [{"name": "BTC-USD", "type": "PERPETUAL", "assetName": "BTC", "collateralAssetName": "USD", "active": True, "status": status, "isRfq": rfq,
            "tradingConfig": {"maxMarketOrderValue": "3000000", "maxLimitOrderValue": "15000000"}}]},
    })


@pytest.mark.asyncio
async def test_extended_rfq_and_suspended_are_never_indicative_estimates():
    for http in (extended_http(rfq=True), extended_http(status="SUSPENDED")):
        with pytest.raises(DepthUnavailable):
            await provider(http).fetch("extended", "BTC-USD")


@pytest.mark.asyncio
async def test_extended_uses_usd_accounting_and_market_not_limit_value_cap():
    depth = await provider(extended_http()).fetch("extended", "BTC-USD")
    assert depth.quote_currency == "USD"
    assert depth.max_quote_notional == Decimal("3000000")
    assert depth.bids[0].quantity == Decimal("0.12345")
    assert depth.venue_at is None


@pytest.mark.asyncio
async def test_rise_human_sizes_resolved_quote_and_explicit_partial_depth():
    http = HTTP({
        "/markets": {"data": {"markets": [{"market_id": "8", "active": True,
            "config": {"name": "BTC/USDC", "unlocked": True}, "base_asset_symbol": "BTC/USDC", "quote_asset_symbol": "USDC"}]}},
        "/orderbook": {"data": {"market_id": "8", "total_bids": 251, "total_asks": 1,
            "bids": [{"price": "100", "quantity": "0.123456789012345678"}],
            "asks": [{"price": "101", "quantity": "2"}]}},
    })
    depth = await provider(http).fetch("rise", "BTC/USDC")
    assert depth.bids[0].quantity == Decimal("0.123456789012345678")
    assert depth.quote_currency == "USDC"


@pytest.mark.asyncio
async def test_binance_and_bybit_use_market_order_caps_not_limit_order_caps():
    binance = HTTP({
        "/exchangeInfo": {"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING", "contractType": "PERPETUAL", "filters": [
            {"filterType": "LOT_SIZE", "maxQty": "900"}, {"filterType": "MARKET_LOT_SIZE", "maxQty": "120"}]}]},
        "/depth": {"bids": [["99", "1"]], "asks": [["101", "2"]], "T": 1791807763567},
    })
    depth = await provider(binance).fetch("binance_perp", "BTCUSDT")
    assert depth.max_quantity == Decimal("120")
    bybit = HTTP({
        "/instruments-info": {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "status": "Trading", "contractType": "LinearPerpetual", "lotSizeFilter": {"maxOrderQty": "900", "maxMktOrderQty": "100"}}]}},
        "/orderbook": {"retCode": 0, "result": {"s": "BTCUSDT", "b": [["99", "1"]], "a": [["101", "2"]], "cts": 1791807763567}},
    })
    depth = await provider(bybit).fetch("bybit_perp", "BTCUSDT")
    assert depth.max_quantity == Decimal("100")


@pytest.mark.asyncio
async def test_hyperliquid_full_precision_quanto_and_hip3_collateral_metadata():
    http = HTTP({
        "metaAndAssetCtxs": [{"collateralToken": 1, "universe": [{"name": "BTC", "maxLeverage": 40}, {"name": "xyz:BTC", "maxLeverage": 20}]}, [{}, {}]],
        "spotMeta": {"tokens": [{"index": 1, "name": "USDT"}]},
        "l2Book": {"coin": "BTC", "time": 1791807763567, "levels": [[{"px": "99", "sz": "1"}], [{"px": "101", "sz": "2"}]]},
    })
    depth = await provider(http).fetch("hyperliquid", "BTC")
    assert depth.quote_currency == "USDC"
    assert depth.max_quote_notional == Decimal("30000000")
    http.responses["l2Book"]["coin"] = "xyz:BTC"
    depth = await provider(http).fetch("hyperliquid_xyz", "xyz:BTC")
    assert depth.quote_currency == "USDT"
    assert depth.max_quote_notional is None


@pytest.mark.asyncio
async def test_bullet_price_currency_is_not_settlement_and_unknown_time_is_not_invented():
    http = HTTP({
        "/exchangeInfo": {"symbols": [{"symbol": "BTC-USD", "baseAsset": "BTC", "quoteAsset": "USDT", "marginAsset": "USDC", "status": "TRADING"}]},
        "/depth": {"T": 1791807763567684, "E": 1791807763567684, "bids": [["99", "1"]], "asks": [["101", "2"]]},
    })
    depth = await provider(http).fetch("bullet", "BTC-USD")
    assert depth.quote_currency == "USDT"
    assert depth.venue_at is None


@pytest.mark.asyncio
async def test_fx_refresh_failure_never_reuses_stale_success_and_books_survive():
    reader = ExecutionReader()
    http = HTTP({"exchange-rates": {"data": {"currency": "USD", "rates": {"USDC": "2", "USDT": "4"}}}})
    reader._clients = {"fx": http}
    reader._fx_lock = asyncio.Lock()
    reader._parallel = asyncio.Semaphore(4)
    rates = await reader._exchange_rates()
    assert rates["rates"] == {"USD": "1", "USDC": "0.5", "USDT": "0.25"}
    reader._fx_at -= 31
    http.responses = {}
    failed = await reader._exchange_rates()
    assert failed["rates"] == {"USD": "1"}
    assert failed["ts"] is None and failed["error"]

    class Provider:
        async def fetch(self, venue, symbol):
            return book(venue)
    reader._provider = Provider()
    result = await reader._capture({"bulk": "BTC-USD"})
    assert len(result["books"]) == 1 and result["fx"]["error"]
    reader.close()


@pytest.mark.asyncio
async def test_overall_deadline_preserves_completed_venues():
    reader = ExecutionReader()
    reader.deadline = 0.02
    reader.individual_timeout = 1
    reader._parallel = asyncio.Semaphore(4)
    class Provider:
        async def fetch(self, venue, symbol):
            if venue == "bullet":
                await asyncio.sleep(10)
            return book(venue)
    reader._provider = Provider()
    async def fx():
        return reader._fx_error("offline")
    reader._exchange_rates = fx
    result = await reader._capture({"bulk": "BTC-USD", "bullet": "BTC-USD"})
    assert [depth.venue for depth in result["books"]] == ["bulk"]
    assert "overall capture deadline" in result["errors"]["bullet"]
    reader.close()


@pytest.mark.asyncio
async def test_cancellation_keeps_admission_until_async_cleanup_finishes():
    reader = ExecutionReader()
    cleanup_started, cleanup_finish = asyncio.Event(), asyncio.Event()
    class Provider:
        async def fetch(self, venue, symbol):
            try:
                await asyncio.sleep(10)
            finally:
                cleanup_started.set()
                await cleanup_finish.wait()
    async def clients():
        reader._provider = Provider()
        reader._parallel = asyncio.Semaphore(4)
    async def fx():
        return reader._fx_error("offline")
    reader._ensure_clients = clients
    reader._exchange_rates = fx
    assert reader._slots.acquire(blocking=False)
    assert reader._slots.acquire(blocking=False)
    admission = _Admission(reader._slots)
    task = asyncio.create_task(reader._bounded({"bulk": "BTC-USD"}, admission))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    task.cancel()  # concurrent caller/shutdown cancellation cannot abort cleanup
    await asyncio.sleep(0)
    assert not reader._slots.acquire(blocking=False)
    cleanup_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reader._slots.acquire(blocking=False)
    reader._slots.release()
    reader._slots.release()
    reader.close()


def test_never_started_admission_cancellation_revokes_work():
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    admission = _Admission(slots)
    future = Future()
    future.add_done_callback(admission.submitted_done)
    future.cancel()
    assert admission.start() is False
    assert slots.acquire(blocking=False)


def test_constructor_is_lazy_two_slots_and_shutdown_closes_all_clients():
    class OfflineReader(ExecutionReader):
        async def _ensure_clients(self):
            self._clients = {"a": HTTP({}), "b": HTTP({})}
            self._parallel = asyncio.Semaphore(4)
        async def _exchange_rates(self):
            return self._fx_error("offline")
    reader = OfflineReader()
    assert reader._loop is None and reader._clients == {}
    assert reader._slots.acquire(blocking=False)
    assert reader._slots.acquire(blocking=False)
    with pytest.raises(ReaderBusy):
        reader.capture({})
    reader._slots.release()
    reader._slots.release()
    result = reader.capture({})
    assert result["books"] == []
    reader.close()
    assert all(client.closed for client in reader._clients.values())
    assert not reader._thread.is_alive()


@pytest.mark.asyncio
async def test_individual_timeout_bounded_parallelism_and_single_book_fetch():
    reader = ExecutionReader()
    reader.individual_timeout = 0.01
    reader._parallel = asyncio.Semaphore(2)
    active = 0
    maximum = 0
    calls = {}
    class Provider:
        async def fetch(self, venue, symbol):
            nonlocal active, maximum
            calls[venue] = calls.get(venue, 0) + 1
            active += 1
            maximum = max(maximum, active)
            try:
                if venue == "bullet":
                    await asyncio.sleep(10)
                else:
                    await asyncio.sleep(0)
                return book(venue)
            finally:
                active -= 1
    async def fx():
        return reader._fx_error("offline")
    reader._provider = Provider()
    reader._exchange_rates = fx
    result = await reader._capture({"bulk": "BTC-USD", "extended": "BTC-USD", "bullet": "BTC-USD"})
    assert maximum <= 2
    assert calls == {"bulk": 1, "extended": 1, "bullet": 1}
    assert len(result["books"]) == 2
    assert result["errors"]["bullet"] == "Public depth request timed out."
    reader.close()
