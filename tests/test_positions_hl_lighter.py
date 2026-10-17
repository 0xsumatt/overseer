from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.positions import PositionReadError
from data_collection.http import HttpError
from data_collection.positions.hyperliquid import HyperliquidPositions
from data_collection.positions.lighter import LighterPositions


ADDRESS = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"
SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)
UNTIL = SINCE + timedelta(days=1)


class HyperliquidHTTP:
    def __init__(self):
        self.funding = []
        self.states = {
            "": {"assetPositions": [{"position": {
                "coin": "BTC", "szi": "-0.25", "positionValue": "25000",
                "entryPx": "99000", "unrealizedPnl": "-250",
            }}], "marginSummary": {"accountValue": "1000"}},
            "cash": {"assetPositions": [{"position": {
                "coin": "cash:TSLA", "szi": "2.5", "positionValue": "1000",
                "entryPx": "390", "unrealizedPnl": "25",
            }}], "marginSummary": {"accountValue": "500"}},
        }
        self.calls = []

    async def post_json(self, url, *, json):
        assert url == "https://api.hyperliquid.xyz/info"
        self.calls.append(json)
        kind = json["type"]
        if kind == "spotMeta":
            return {"tokens": [{"index": 0, "name": "USDC"}, {"index": 1, "name": "USDT"}]}
        if kind == "perpDexs":
            return [None, {"name": "cash"}]
        if kind == "metaAndAssetCtxs":
            dex = json["dex"]
            return [{"universe": [{"name": "cash:TSLA" if dex else "BTC"}],
                     "collateralToken": 1 if dex else 0}, [{}]]
        if kind == "userAbstraction":
            return "unifiedAccount"
        if kind == "clearinghouseState":
            return deepcopy(self.states[json["dex"]])
        if kind == "spotClearinghouseState":
            return {"balances": [{"token": 1, "coin": "USDT", "total": "123.456789"}]}
        if kind == "userFunding":
            return self.funding.pop(0)
        raise AssertionError("Unexpected info type")


def hl_funding(offset, *, symbol="cash:TSLA", amount="1.23", identifier=None):
    return {"time": int(SINCE.timestamp() * 1000) + offset,
            "hash": identifier or f"hash-{offset}",
            "delta": {"type": "funding", "coin": symbol, "usdc": amount}}


@pytest.mark.asyncio
async def test_hyperliquid_reads_every_dex_and_preserves_native_money():
    http = HyperliquidHTTP()
    snapshot = await HyperliquidPositions(http).fetch_positions(ADDRESS)
    native, hip3 = snapshot.positions
    assert native.quantity == Decimal("-0.25")
    assert native.unrealized_pnl == Decimal("-250")
    assert hip3.quantity == Decimal("2.5")
    assert hip3.symbol == "cash:TSLA"
    assert hip3.scope == "cash"
    assert hip3.quote_currency == hip3.settlement_currency == "USDT"
    assert hip3.collateral_currencies == ("USDT",)
    assert hip3.unrealized_pnl == Decimal("25")
    assert hip3.mark_price == Decimal("400")
    assert snapshot.balances[0].amount == Decimal("123.456789")
    assert not snapshot.equity  # Unified balance must not be counted once per DEX.


@pytest.mark.asyncio
async def test_hyperliquid_malformed_hip3_cannot_look_flat():
    http = HyperliquidHTTP()
    http.states["cash"] = {"marginSummary": {"accountValue": "0"}}
    with pytest.raises(PositionReadError):
        await HyperliquidPositions(http).fetch_positions(ADDRESS)


@pytest.mark.asyncio
async def test_hyperliquid_inclusive_pagination_deduplicates_boundary_and_restores_window():
    http = HyperliquidHTTP()
    first = [hl_funding(i) for i in range(500)]
    http.funding = [first, [first[-1], hl_funding(500, amount="-0.77")]]
    adapter = HyperliquidPositions(http)
    page = await adapter.fetch_funding(ADDRESS, SINCE, UNTIL)
    assert not page.complete and page.coverage_since is None
    assert len(page.events) == 500
    assert page.events[0].currency == "USDT"
    assert page.events[0].amount == Decimal("1.23")
    continuation = await HyperliquidPositions(http).fetch_funding(
        ADDRESS, SINCE + timedelta(hours=1), UNTIL + timedelta(hours=1), cursor=page.cursor,
    )
    assert continuation.complete and continuation.cursor is None
    assert continuation.coverage_since == SINCE
    assert len(continuation.events) == 1
    assert continuation.events[0].amount == Decimal("-0.77")


@pytest.mark.asyncio
async def test_hyperliquid_nonprogress_page_is_error_not_completion():
    http = HyperliquidHTTP()
    page_rows = [hl_funding(1, identifier=f"hash-{i}") for i in range(500)]
    http.funding = [page_rows, page_rows]
    adapter = HyperliquidPositions(http)
    first = await adapter.fetch_funding(ADDRESS, SINCE, UNTIL)
    with pytest.raises(PositionReadError):
        await adapter.fetch_funding(ADDRESS, SINCE, UNTIL, cursor=first.cursor)


class LighterHTTP:
    def __init__(self):
        self.calls = []
        self.funding = []
        self.account = {
            "code": 0, "index": 123, "account_trading_mode": 1,
            "positions": [{"market_id": 4, "symbol": "1000PEPE", "sign": -1,
                           "position": "12.5", "position_value": "25",
                           "avg_entry_price": "2.1", "unrealized_pnl": "1.25"}],
            "assets": [{"asset_id": 1, "symbol": "ETH", "balance": "0.12345678"}],
            "total_asset_value": "99.123456",
        }

    async def get_json(self, url, *, params=None, headers=None):
        assert url.startswith("https://mainnet.zklighter.elliot.ai/api/v1/")
        self.calls.append((url, params, headers))
        endpoint = url.rsplit("/", 1)[-1]
        if endpoint == "assetDetails":
            return {"code": 200, "asset_details": [
                {"asset_id": 3, "symbol": "USDC", "margin_mode": "enabled"},
                {"asset_id": 1, "symbol": "ETH", "margin_mode": "enabled"},
                {"asset_id": 11, "symbol": "XAUT", "margin_mode": "enabled"},
            ]}
        if endpoint == "orderBookDetails":
            return {"code": 200, "order_book_details": [
                {"market_id": 4, "symbol": "1000PEPE", "market_type": "perp", "multiplier": "1"}
            ], "spot_order_book_details": []}
        if endpoint == "account":
            return {"code": 200, "total": 1, "accounts": [self.account], "next_cursor": ""}
        if endpoint == "positionFunding":
            result = self.funding.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        raise AssertionError("Unexpected endpoint")


def lighter_funding(identifier=1, amount="0.123456"):
    return {"timestamp": int(SINCE.timestamp()) + 3600, "market_id": 4,
            "funding_id": identifier, "change": amount}


@pytest.mark.asyncio
async def test_lighter_unsigned_human_size_times_sign_and_separate_spot():
    snapshot = await LighterPositions(LighterHTTP()).fetch_positions("000123")
    [position] = snapshot.positions
    assert position.quantity == Decimal("-12.5")
    assert position.symbol == "1000PEPE"  # Unknown contract multipliers stay separate.
    assert position.unrealized_pnl == Decimal("1.25")
    assert position.quote_currency == position.settlement_currency == "USDC"
    assert position.collateral_currencies == ("ETH", "USDC", "XAUT")
    assert snapshot.balances[0].currency == "ETH"
    assert snapshot.balances[0].amount == Decimal("0.12345678")
    assert snapshot.equity[0].amount == Decimal("99.123456")


@pytest.mark.asyncio
async def test_lighter_header_only_read_token_and_durable_page_cursor():
    http = LighterHTTP()
    http.funding = [
        {"code": 200, "position_fundings": [lighter_funding(), lighter_funding()], "next_cursor": "next-1"},
        {"code": 200, "position_fundings": [lighter_funding(2, "-0.654321")], "next_cursor": ""},
    ]
    token = "ro:123:single:2000000000:abcdef"
    first = await LighterPositions(http).fetch_funding("123", SINCE, UNTIL, token=token)
    assert not first.complete and len(first.events) == 1
    assert first.events[0].amount == Decimal("0.123456")
    assert token not in repr(first.cursor)
    last = await LighterPositions(http).fetch_funding(
        "123", SINCE + timedelta(hours=1), UNTIL + timedelta(hours=1), cursor=first.cursor, token=token,
    )
    assert last.complete and last.coverage_since == SINCE
    assert last.events[0].amount == Decimal("-0.654321")
    url, params, _ = http.calls[-1]
    assert token not in url and token not in repr(params)


@pytest.mark.asyncio
async def test_lighter_malformed_account_is_not_flat_and_private_error_is_safe():
    http = LighterHTTP()
    del http.account["positions"]
    with pytest.raises(PositionReadError):
        await LighterPositions(http).fetch_positions("123")
    secret = "ro:123:single:2000000000:abcdef"
    http.funding = [HttpError(401, secret, "private response body")]
    with pytest.raises(PositionReadError) as error:
        await LighterPositions(http).fetch_funding("123", SINCE, UNTIL, token=secret)
    assert secret not in str(error.value) and "private response body" not in str(error.value)


@pytest.mark.asyncio
async def test_lighter_nonprogress_and_trading_tokens_rejected():
    http = LighterHTTP()
    http.funding = [{"code": 200, "position_fundings": [lighter_funding()], "next_cursor": "same"}] * 2
    adapter = LighterPositions(http)
    first = await adapter.fetch_funding("123", SINCE, UNTIL)
    with pytest.raises(PositionReadError):
        await adapter.fetch_funding("123", SINCE, UNTIL, cursor=first.cursor)
    with pytest.raises(PositionReadError):
        await adapter.fetch_funding("123", SINCE, UNTIL, token="expiry:123:4:secret")


class WalletHTTP:
    def __init__(self, response):
        self.response = response

    async def get_json(self, url, *, params=None, headers=None):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


WALLET = "0x" + "a" * 40


def wallet_response(rows, cursor=None):
    return {"code": 200, "l1_address": WALLET, "sub_accounts": rows, "next_cursor": cursor}


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"l1_address": "0x" + "b" * 40},
    {"index": -1}, {"index": 2**48}, {"index": True},
    {"status": True}, {"status": 2}, {"code": 403},
])
async def test_lighter_wallet_lookup_rejects_foreign_or_invalid_account_choices(changes):
    row = {"code": 0, "index": 123, "l1_address": WALLET, "status": 1}
    row.update(changes)
    adapter = LighterPositions(WalletHTTP(wallet_response([row])))
    with pytest.raises(PositionReadError):
        await adapter.resolve_accounts(WALLET)


@pytest.mark.asyncio
async def test_lighter_wallet_lookup_rejects_duplicate_choices_and_nonprogress_pages():
    row = {"code": 0, "index": 123, "l1_address": WALLET, "status": 0}
    with pytest.raises(PositionReadError):
        await LighterPositions(WalletHTTP(wallet_response([row, row]))).resolve_accounts(WALLET)
    with pytest.raises(PositionReadError):
        await LighterPositions(WalletHTTP(wallet_response([row], "same"))).resolve_accounts(WALLET, cursor="same")


@pytest.mark.asyncio
async def test_lighter_absent_wallet_is_distinct_from_unavailable_lookup():
    missing = HttpError(400, "client error", '{"code":21100,"message":"account not found"}')
    result = await LighterPositions(WalletHTTP(missing)).resolve_accounts(WALLET)
    assert result["accounts"] == [] and result["next_cursor"] is None
    for failure in [HttpError(503, "server error"), HttpError(400, "client error", '{"code":21103}')]:
        with pytest.raises(PositionReadError):
            await LighterPositions(WalletHTTP(failure)).resolve_accounts(WALLET)


class HyperliquidDiscoveryHTTP:
    def __init__(self, response):
        self.response = response

    async def post_json(self, url, *, json):
        assert json == {"type": "subAccounts", "user": ADDRESS}
        return deepcopy(self.response)


@pytest.mark.asyncio
@pytest.mark.parametrize("children", [None, []])
async def test_hyperliquid_no_children_still_discovers_main_account(children):
    http = HyperliquidDiscoveryHTTP(children)
    result = await HyperliquidPositions(http).resolve_accounts(ADDRESS)
    assert [row["address"] for row in result["accounts"]] == [ADDRESS]
    assert result["next_cursor"] is None


@pytest.mark.asyncio
async def test_hyperliquid_owned_children_are_distinct_native_account_addresses():
    child = "0x" + "ab" * 20
    http = HyperliquidDiscoveryHTTP([{
        "name": "Child", "master": ADDRESS.upper().replace("0X", "0x"),
        "subAccountUser": child.upper().replace("0X", "0x"),
        "clearinghouseState": {"assetPositions": []},
    }])
    result = await HyperliquidPositions(http).resolve_accounts(ADDRESS)
    rows = {row["address"]: row for row in result["accounts"]}
    assert set(rows) == {ADDRESS, child}
    assert rows[child]["name"] == "Child"
    assert result["next_cursor"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("children", [
    {},
    [{"master": WALLET, "subAccountUser": "0x" + "b" * 40, "name": "Foreign"}],
    [{"master": ADDRESS, "subAccountUser": "not-an-address", "name": "Malformed"}],
    [{"master": ADDRESS, "name": "Missing address"}],
    [{"subAccountUser": WALLET, "name": "Missing owner"}],
    [{"master": ADDRESS, "subAccountUser": ADDRESS, "name": "Duplicate main"}],
    [{"master": ADDRESS, "subAccountUser": WALLET, "name": "One"},
     {"master": ADDRESS, "subAccountUser": WALLET.upper().replace("0X", "0x"), "name": "Two"}],
])
async def test_hyperliquid_discovery_rejects_unbound_malformed_and_duplicate_accounts(children):
    with pytest.raises(PositionReadError):
        await HyperliquidPositions(HyperliquidDiscoveryHTTP(children)).resolve_accounts(ADDRESS)


@pytest.mark.asyncio
async def test_lighter_discovery_normalizes_owned_indices_and_advancing_cursor():
    http = WalletHTTP(wallet_response([
        {"code": 0, "index": 123, "l1_address": WALLET, "status": 1},
        {"code": 0, "index": 456, "l1_address": WALLET, "status": 0},
    ], "page-2"))
    adapter = LighterPositions(http)
    first = await adapter.resolve_accounts(WALLET)
    assert [row["address"] for row in first["accounts"]] == ["123", "456"]
    assert first["next_cursor"] == "page-2"
    http.response = wallet_response([
        {"code": 0, "index": 789, "l1_address": WALLET, "status": 1},
    ])
    second = await adapter.resolve_accounts(WALLET, cursor=first["next_cursor"])
    assert [row["address"] for row in second["accounts"]] == ["789"]
    assert second["next_cursor"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"code": 200, "l1_address": "0x" + "b" * 40, "sub_accounts": []},
    {"code": 200, "l1_address": WALLET, "sub_accounts": None},
    {"code": 200, "l1_address": WALLET, "sub_accounts": [], "next_cursor": "page-2"},
])
async def test_lighter_discovery_rejects_wrong_envelope_owner_and_empty_nonterminal_page(response):
    with pytest.raises(PositionReadError):
        await LighterPositions(WalletHTTP(response)).resolve_accounts(WALLET)


@pytest.mark.asyncio
async def test_lighter_wallet_discovery_normalizes_evm_case():
    row = {"code": 0, "index": 123, "l1_address": WALLET.upper().replace("0X", "0x"), "status": 1}
    result = await LighterPositions(WalletHTTP(wallet_response([row]))).resolve_accounts(
        WALLET.upper().replace("0X", "0x"),
    )
    assert result["wallet"] == WALLET
    assert [account["address"] for account in result["accounts"]] == ["123"]


@pytest.mark.asyncio
async def test_lighter_known_index_is_one_normalized_account_with_discovery_limit():
    result = await LighterPositions(WalletHTTP(AssertionError("Numeric discovery must not query wallet ownership."))).resolve_accounts("000123")
    assert result["wallet"] == "123"
    assert [row["address"] for row in result["accounts"]] == ["123"]
    assert result["next_cursor"] is None
    assert isinstance(result.get("note"), str) and result["note"]


@pytest.mark.asyncio
async def test_lighter_numeric_index_does_not_accept_wallet_pagination_cursor():
    with pytest.raises(PositionReadError):
        await LighterPositions(WalletHTTP({})).resolve_accounts("123", cursor="page-2")


@pytest.mark.asyncio
async def test_hyperliquid_discovery_rejects_unsupported_cursor():
    with pytest.raises(PositionReadError):
        await HyperliquidPositions(HyperliquidDiscoveryHTTP([])).resolve_accounts(ADDRESS, cursor="page-2")
