from datetime import datetime, timezone
from decimal import Decimal

import pytest

from core.positions import PositionReadError
from data_collection.positions.bulk import BulkPositions
from data_collection.positions.risex import RisexPositions

BULK = "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7"
ISO = "11111111111111111111111111111111"
RISE = "0x938a104179a2D23DeC249A968ecd5B4F8B857cac"
TOKEN = "0xe436820ba0c69702c1d3e601d421c0ef38262739"
SINCE = datetime(2026, 10, 10, tzinfo=timezone.utc)
UNTIL = datetime(2026, 10, 11, tzinfo=timezone.utc)


class HTTP:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    async def get_json(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses[url.rsplit("/", 1)[-1]]

    async def post_json(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses[kwargs["json"]["type"]]


def rise_http(**responses):
    return HTTP({
        "config": {"data": {"addresses": {"usdc": TOKEN}}},
        "markets": {"data": {"markets": [{"market_id": "2", "config": {"quote": TOKEN, "name": "ETH/USDC"}}]}},
        **responses,
    })


@pytest.mark.asyncio
async def test_bulk_isolated_positions_and_aggregate_are_counted_once():
    http = HTTP({
        "exchangeInfo": [{"symbol": "BTC-USD", "quoteAsset": "USD"}],
        "fullAccount": [{"fullAccount": {
            "positions": [
                {"symbol": "BTC-USD", "size": "0.5", "iso": False, "unrealizedPnl": "2"},
                {"symbol": "BTC-USD", "size": "-0.2", "iso": True, "isoPubkey": ISO},
            ],
            "margin": {"totalMargin": "1000", "aggTotalMargin": "1250"},
            "subAccounts": [{"pubkey": ISO}],
        }}],
    })
    snapshot = await BulkPositions(http).fetch_positions(BULK)
    assert [p.quantity for p in snapshot.positions] == [Decimal("0.5"), Decimal("-0.2")]
    assert [p.scope for p in snapshot.positions] == ["main", ISO]
    assert all(p.quote_currency == "USD" and p.settlement_currency == "USDC" for p in snapshot.positions)
    assert snapshot.positions[0].pnl_currency == "USD"
    assert snapshot.equity[0].currency == "UNSPECIFIED"
    assert snapshot.equity[0].amount == Decimal("1250")
    http.responses["fullAccount"] = []
    with pytest.raises(PositionReadError):
        await BulkPositions(http).fetch_positions(BULK)


@pytest.mark.asyncio
async def test_risex_portfolio_human_units_not_raw_position_endpoint():
    http = rise_http(details={"data": {
        "account": RISE,
        "summary": {"usdc_balance": "1000.25", "total_account_value": "1028.594758958663649308"},
        "positions": [{"market_id": "2", "size": "-9.026", "margin_mode": 1,
                       "avg_entry_price": "2508.512690527249067373", "mark_price": "2505.372344974549793131",
                       "unrealized_pnl": "28.344758958663649308"}],
    }})
    snapshot = await RisexPositions(http).fetch_positions(RISE)
    position = snapshot.positions[0]
    assert position.quantity == Decimal("-9.026")
    assert position.entry_price == Decimal("2508.512690527249067373")
    assert position.scope == "isolated:2"
    assert snapshot.equity[0].amount == Decimal("1028.594758958663649308")


@pytest.mark.asyncio
async def test_risex_settled_only_sign_and_durable_fixed_window():
    row = {"market_id": "2", "funding_payment": "0.03413795651635929", "position_size": "1974",
           "block_number": "24053513", "block_time": "1791658800000000000", "tx_hash": "0xabc",
           "side": "long", "trade_side": "BUY", "is_settled": True, "entry_type": "funding"}
    data = {"account": RISE, "records": [row, {**row, "is_settled": False}, {**row, "entry_type": "dividend"}],
            "page": 1, "has_next_page": True}
    http = rise_http(**{"funding-payments": {"data": data}})
    adapter = RisexPositions(http)
    page = await adapter.fetch_funding(RISE, SINCE, UNTIL)
    assert len(page.events) == 1
    assert page.events[0].amount == Decimal("-0.03413795651635929")
    assert page.events[0].currency == "USDC"
    assert not page.complete and page.cursor["page"] == 2
    data["page"] = 2
    with pytest.raises(PositionReadError):
        await adapter.fetch_funding(RISE, SINCE, UNTIL, cursor=page.cursor)
    data["page"] = 1
    repeated = await adapter.fetch_funding(RISE, SINCE, UNTIL)
    assert repeated.events[0].event_id == page.events[0].event_id


@pytest.mark.asyncio
async def test_bulk_funding_native_usd_opaque_cursor_and_retention():
    http = HTTP({"fundingHistory": {
        "data": [{"owner": BULK, "symbol": "BTC-USD", "payment": "-0.125", "slot": 3,
                  "sequence": 2, "timestamp": 1791658800000000000, "iso": True, "isoPubkey": ISO}],
        "page": {"hasMore": True, "nextCursor": "opaque", "coverage": "partial", "startSlot": 1,
                 "endSlot": 4, "asOfSlot": 4, "minAvailableSlot": 1},
    }})
    adapter = BulkPositions(http)
    page = await adapter.fetch_funding(BULK, SINCE, UNTIL)
    assert page.events[0].amount == Decimal("-0.125")
    assert page.events[0].currency == "USD" and page.events[0].scope == ISO
    assert page.coverage_since == UNTIL
    assert "retained slot" in page.coverage_note
    with pytest.raises(PositionReadError):
        await adapter.fetch_funding(BULK, SINCE, UNTIL, cursor=page.cursor)


@pytest.mark.asyncio
async def test_bulk_discovers_owned_subaccounts_not_isolated_position_lanes():
    child = "So11111111111111111111111111111111111111112"
    http = HTTP({"fullAccount": [{"fullAccount": {
        "positions": [{"symbol": "BTC-USD", "size": "1", "iso": True, "isoPubkey": ISO}],
        "subAccounts": [{"pubkey": child}],
    }}]})
    result = await BulkPositions(http).resolve_accounts(BULK)
    addresses = [row["address"] for row in result["accounts"]]
    assert set(addresses) == {BULK, child}
    assert len(addresses) == 2
    assert ISO not in addresses
    assert result["next_cursor"] is None


@pytest.mark.asyncio
async def test_bulk_without_children_includes_main_but_never_isolated_lanes():
    http = HTTP({"fullAccount": [{"fullAccount": {
        "subAccounts": [],
        "positions": [{"symbol": "BTC-USD", "size": "1", "iso": True, "isoPubkey": ISO}],
    }}]})
    result = await BulkPositions(http).resolve_accounts(BULK)
    assert [row["address"] for row in result["accounts"]] == [BULK]
    assert result["next_cursor"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("children", [
    None, {}, ["So11111111111111111111111111111111111111112"],
    [{}], [{"pubkey": "invalid"}], [{"pubkey": BULK}],
    [{"pubkey": ISO}, {"pubkey": ISO}],
])
async def test_bulk_malformed_or_duplicate_owned_children_never_become_partial_success(children):
    http = HTTP({"fullAccount": [{"fullAccount": {"subAccounts": children}}]})
    with pytest.raises(PositionReadError):
        await BulkPositions(http).resolve_accounts(BULK)


@pytest.mark.asyncio
async def test_bulk_missing_account_is_not_successful_main_only_discovery():
    with pytest.raises(PositionReadError):
        await BulkPositions(HTTP({"fullAccount": []})).resolve_accounts(BULK)


@pytest.mark.asyncio
async def test_risex_direct_address_discovery_is_canonical_and_explicitly_limited():
    result = await RisexPositions(HTTP({})).resolve_accounts(RISE)
    assert [row["address"] for row in result["accounts"]] == [RISE.lower()]
    assert result["next_cursor"] is None
    assert isinstance(result.get("note"), str) and result["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter,address", [(BulkPositions, BULK), (RisexPositions, RISE)])
async def test_nonpaginated_discovery_rejects_cursor_instead_of_repeating_accounts(adapter, address):
    with pytest.raises(PositionReadError):
        await adapter(HTTP({})).resolve_accounts(address, cursor="page-2")
