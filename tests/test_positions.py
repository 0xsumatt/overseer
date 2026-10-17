from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from core.positions import PositionReadError
from core.position_tokens import validate_token
from core.symbols import SymbolRegistry
from flask import Flask
from web.position_reader import PositionReader
from web.views.positions import bp
from web.positions import position_payload

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
REGISTRY = SymbolRegistry.from_config({"assets": {"BTC": {"bulk": "BTC-USD", "lighter": "BTC"}, "ETH": {"bulk": "ETH-USD"}}})


def account(account_id, venue, positions=(), **changes):
    row = {
        "id": account_id, "venue": venue, "address": str(account_id), "label": venue,
        "snapshot": {"positions": list(positions)}, "created_at": NOW,
        "positions_checked_at": NOW, "positions_attempted_at": NOW, "positions_error": None,
        "funding_checked_at": NOW, "funding_attempted_at": NOW, "funding_error": None,
        "funding_since": NOW - timedelta(days=40), "funding_cursor": None,
    }
    row.update(changes)
    return row


def position(symbol, quantity, mark="100", quote="USD", pnl=None, pnl_currency=None):
    return {
        "symbol": symbol, "quantity": quantity, "mark_price": mark, "entry_price": None,
        "quote_currency": quote, "settlement_currency": "USDC", "collateral_currencies": ["USDC"],
        "unrealized_pnl": pnl, "pnl_currency": pnl_currency,
    }


def payment(account_id, symbol, amount, currency, **changes):
    row = {"account_id": account_id, "symbol": symbol, "amount": Decimal(amount), "currency": currency, "ts": NOW - timedelta(hours=1)}
    row.update(changes)
    return row


def test_common_reference_and_native_denominations_are_not_pegged_or_relabelled():
    accounts = [
        account(1, "bulk", [position("BTC-USD", "2", pnl="7", pnl_currency="USD")]),
        account(2, "lighter", [position("BTC", "-0.5", mark="110", quote="USDC", pnl="2")]),
    ]
    events = [payment(1, "BTC-USD", "5", "USD"), payment(2, "BTC", "-1", "USDC")]
    result = position_payload(accounts, events, {"ts": NOW, "rates": {"USDC": "0.9"}}, REGISTRY, NOW, 24)
    btc = result["assets"][0]
    assert Decimal(btc["reference_price_usd"]) == Decimal("99.5")
    assert Decimal(btc["net_delta_usd"]) == Decimal("149.25")
    assert btc["unrealized_pnl"] == [{"currency": "USD", "amount": "7"}, {"currency": "USDC", "amount": "2"}]
    assert result["totals"]["funding"] == [{"currency": "USD", "amount": "5"}, {"currency": "USDC", "amount": "-1"}]
    assert Decimal(result["totals"]["funding_usd"]) == Decimal("4.1")
    assert btc["legs"][0]["settlement_currency"] == "USDC"
    assert btc["legs"][0]["unrealized_pnl_currency"] == "USD"


def test_account_market_funding_is_not_misattributed_to_an_extra_cross_margin_leg():
    isolated = {**position("ETH", "-2", quote="USDC"), "scope": "isolated:2"}
    rows = [account(1, "risex", [isolated])]
    events = [payment(1, "ETH", "3", "USDC", scope="account-market")]
    result = position_payload(rows, events, {"ts": NOW, "rates": {"USDC": "0.9"}}, REGISTRY, NOW, 24)
    eth = result["assets"][0]
    assert eth["net_quantity"] == "-2"
    assert len(eth["legs"]) == 1
    assert eth["legs"][0]["scope"] == "isolated:2"
    assert eth["legs"][0]["funding_scope"] == "account-market"
    assert eth["legs"][0]["funding"] == [{"currency": "USDC", "amount": "3"}]
    assert result["totals"]["funding_usd"] == "2.7"


def test_equal_dollars_on_different_assets_do_not_become_a_hedge():
    rows = [account(1, "bulk", [position("BTC-USD", "1"), position("ETH-USD", "-1")])]
    result = position_payload(rows, [], None, REGISTRY, NOW, 24)
    assert result["totals"]["net_delta_usd"] == "200"
    assert {row["asset"]: row["net_quantity"] for row in result["assets"]} == {"BTC": "1", "ETH": "-1"}


def test_position_failure_retains_exposure_but_does_not_invalidate_settled_ledger():
    rows = [account(1, "bulk", [position("BTC-USD", "1")], positions_error="Read failed.")]
    result = position_payload(rows, [payment(1, "BTC-USD", "3", "USD")], None, REGISTRY, NOW, 24)
    assert result["accounts"][0]["positions"]["state"] == "error"
    assert result["assets"][0]["net_quantity"] == "1"
    assert result["totals"]["positions_complete"] is False
    assert result["totals"]["net_delta_usd"] is None
    assert result["totals"]["funding_usd"] == "3"


def test_funding_catchup_preserves_closed_market_payments_without_false_exposure():
    rows = [account(1, "lighter", snapshot=None, positions_checked_at=None,
                    funding_cursor={"cursor": {"page": 2}})]
    events = [payment(1, "BTC", "2", "USDC"), payment(9, "BTC", "999", "USD"),
              payment(1, "BTC", "800", "USDC", ts=NOW + timedelta(hours=1))]
    result = position_payload(rows, events, {"ts": NOW, "rates": {"USDC": "0.99"}}, REGISTRY, NOW, 24)
    assert result["totals"]["funding"] == [{"currency": "USDC", "amount": "2"}]
    assert result["totals"]["funding_complete"] is False
    assert result["totals"]["funding_usd"] is None
    assert result["assets"][0]["net_quantity"] is None
    assert result["assets"][0]["legs"][0]["quantity"] is None
    assert result["accounts"][0]["funding"]["catching_up"] is True


def test_missing_or_stale_fx_never_implies_a_stablecoin_dollar_peg():
    rows = [account(1, "lighter", [position("BTC", "1", quote="USDC")])]
    events = [payment(1, "BTC", "4", "USDC")]
    result = position_payload(rows, events, {"ts": NOW - timedelta(minutes=6), "rates": {"USDC": "1"}}, REGISTRY, NOW, 24)
    assert result["totals"]["funding_complete"] is True
    assert result["totals"]["funding_usd"] is None
    assert result["totals"]["net_delta_usd"] is None
    assert result["totals"]["funding"] == [{"currency": "USDC", "amount": "4"}]


@pytest.mark.parametrize("token", ["0x" + "a" * 64, "signer-private-key", "ro:2:single:9999999999:" + "a" * 32, "ro:1:single:1:" + "a" * 32])
def test_lighter_credentials_must_be_unexpired_read_only_and_account_scoped(token):
    with pytest.raises(PositionReadError):
        validate_token(token, "1")


@pytest.fixture
def anonymous_app():
    app = Flask(__name__)
    app.register_blueprint(bp)
    app.extensions["symbols"] = REGISTRY
    yield app
    reader = app.extensions.get("position_reader")
    if reader is not None:
        reader.close()


def aggregate_body():
    return {"hours": 24, "accounts": [], "events": [], "fx": None}


def test_anonymous_aggregate_has_no_database_login_cookie_or_server_watchlist(anonymous_app):
    first = anonymous_app.test_client()
    second = anonymous_app.test_client()
    body = aggregate_body()
    now = datetime.now(timezone.utc).isoformat()
    body["accounts"] = [{
        "id": "aabbccdd-1111-4444-8888-aabbccddeeff", "venue": "lighter", "address": "123",
        "label": "My tab", "created_at": now, "snapshot": None, "has_read_only_token": True,
    }]
    response = first.post("/api/positions/aggregate", json=body)
    assert response.status_code == 200
    assert response.get_json()["accounts"][0]["address"] == "123"
    assert response.get_json()["accounts"][0]["has_read_only_token"] is True
    assert "token_ciphertext" not in response.get_json()["accounts"][0]
    assert "csrf_token" not in response.get_json()
    assert "Set-Cookie" not in response.headers
    assert response.cache_control.no_store
    assert second.post("/api/positions/aggregate", json=aggregate_body()).get_json()["accounts"] == []
    assert first.post("/api/positions/aggregate", json=aggregate_body()).get_json()["accounts"] == []
    for method, path in [("get", "/api/positions"),
                         ("patch", "/api/positions/accounts/1"), ("delete", "/api/positions/accounts/1")]:
        assert getattr(first, method)(path).status_code == 404


@pytest.mark.parametrize("credential", ["0x" + "a" * 64, "trading-private-key", "ro:2:single:9999999999:" + "a" * 32])
def test_credentials_rejected_without_echo_or_cookie(anonymous_app, credential):
    client = anonymous_app.test_client()
    response = client.post("/api/positions/read", json={
        "venue": "lighter", "address": "123", "kind": "funding", "read_only_token": credential,
    })
    assert response.status_code == 400
    assert credential.encode() not in response.data
    assert "Set-Cookie" not in response.headers
    discovery = client.post("/api/positions/accounts", json={
        "venue": "lighter", "wallet": "0x" + "a" * 40, "read_only_token": credential,
    })
    assert discovery.status_code == 400
    assert credential.encode() not in discovery.data
    assert discovery.cache_control.no_store


class DiscoveryReader:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def close(self):
        pass

    def discover_accounts(self, venue, wallet, cursor=None):
        self.calls.append((venue, wallet, cursor))
        if self.failure:
            raise self.failure
        address = "123" if venue == "lighter" else wallet
        return {"venue": venue, "wallet": wallet, "accounts": [{"address": address, "name": "Main"}],
                "next_cursor": None if cursor else "next-page", "note": "Discovery is limited."}


@pytest.mark.parametrize("venue,wallet,canonical", [
    ("hyperliquid", " 0x" + "Ab" * 20 + " ", "0x" + "ab" * 20),
    ("risex", "0x" + "AB" * 20, "0x" + "ab" * 20),
    ("lighter", "0x" + "AB" * 20, "0x" + "ab" * 20),
    ("lighter", "000123", "123"),
    ("bulk", "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7",
     "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7"),
])
def test_generic_discovery_normalizes_wallet_and_preserves_page_and_notice(anonymous_app, venue, wallet, canonical):
    reader = DiscoveryReader()
    anonymous_app.extensions["position_reader"] = reader
    client = anonymous_app.test_client()
    first = client.post("/api/positions/accounts", json={"venue": venue, "wallet": wallet})
    assert first.status_code == 200
    data = first.get_json()
    assert data["venue"] == venue and data["wallet"] == canonical
    assert data["accounts"][0]["address"] == ("123" if venue == "lighter" else canonical)
    assert data["next_cursor"] == "next-page"
    assert data["note"]
    second = client.post("/api/positions/accounts", json={
        "venue": venue, "wallet": wallet, "cursor": data["next_cursor"],
    })
    assert second.status_code == 200 and second.get_json()["next_cursor"] is None
    assert reader.calls == [(venue, canonical, None), (venue, canonical, "next-page")]
    for response in (first, second):
        assert response.cache_control.no_store
        assert "Set-Cookie" not in response.headers
    for browser in (client, anonymous_app.test_client()):
        assert browser.post("/api/positions/aggregate", json=aggregate_body()).get_json()["accounts"] == []
    assert client.post("/api/positions/lighter/accounts", json={"wallet": canonical}).status_code == 404


@pytest.mark.parametrize("body", [
    {}, {"wallet": "0x" + "a" * 40},
    {"venue": "unknown", "wallet": "0x" + "a" * 40},
    {"venue": "hyperliquid", "wallet": "123"},
    {"venue": "risex", "wallet": "0x123"},
    {"venue": "bulk", "wallet": "0x" + "a" * 40},
    {"venue": "lighter", "wallet": "-1"},
    {"venue": "lighter", "wallet": str(2**48)},
    {"venue": "lighter", "wallet": 123},
    {"venue": "lighter", "wallet": "123", "cursor": {}},
    {"venue": "lighter", "wallet": "123", "cursor": ""},
    {"venue": "lighter", "wallet": "123", "cursor": "x" * 2049},
    {"venue": "lighter", "wallet": "123", "address": "123"},
])
def test_discovery_invalid_requests_do_not_reach_reader(anonymous_app, body):
    reader = DiscoveryReader()
    anonymous_app.extensions["position_reader"] = reader
    response = anonymous_app.test_client().post("/api/positions/accounts", json=body)
    assert response.status_code == 400
    assert response.cache_control.no_store
    assert reader.calls == []
    assert "Set-Cookie" not in response.headers


def test_discovery_provider_errors_are_safe_and_never_report_partial_success(anonymous_app):
    secret = "provider-private-response-body"
    reader = DiscoveryReader(RuntimeError(secret))
    anonymous_app.extensions["position_reader"] = reader
    response = anonymous_app.test_client().post("/api/positions/accounts", json={
        "venue": "hyperliquid", "wallet": "0x" + "a" * 40,
    })
    assert response.status_code == 502
    assert response.get_json().get("error")
    assert "accounts" not in response.get_json()
    assert secret.encode() not in response.data
    assert response.cache_control.no_store
    assert "Set-Cookie" not in response.headers


def test_same_symbol_subaccounts_keep_legs_and_once_counted_funding_separate(anonymous_app):
    now = datetime.now(timezone.utc)
    ids = ["aabbccdd-1111-4444-8888-aabbccddeef1", "aabbccdd-1111-4444-8888-aabbccddeef2"]
    body = aggregate_body()
    for index, quantity in enumerate(("2", "-0.5")):
        body["accounts"].append({
            "id": ids[index], "venue": "hyperliquid", "address": "0x" + str(index + 1) * 40,
            "label": "Account", "created_at": (now - timedelta(days=1)).isoformat(),
            "has_read_only_token": False,
            "snapshot": {"positions": [position("BTC", quantity)], "balances": [], "equity": [], "notes": []},
        })
        body["events"].append({
            "account_id": ids[index], "event_id": "same-native-event-id", "symbol": "BTC",
            "amount": ("3", "-1")[index], "currency": "USD",
            "ts": (now - timedelta(hours=1)).isoformat(),
        })
    body["events"].append(dict(body["events"][0]))
    client = anonymous_app.test_client()
    response = client.post("/api/positions/aggregate", json=body)
    assert response.status_code == 200
    data = response.get_json()
    btc = data["assets"][0]
    assert btc["net_quantity"] == "1.5"
    assert data["totals"]["funding"] == [{"currency": "USD", "amount": "2"}]
    legs = {leg["account_id"]: leg for leg in btc["legs"]}
    assert set(legs) == set(ids)
    assert legs[ids[0]]["quantity"] == "2"
    assert legs[ids[0]]["funding"] == [{"currency": "USD", "amount": "3"}]
    assert legs[ids[1]]["quantity"] == "-0.5"
    assert legs[ids[1]]["funding"] == [{"currency": "USD", "amount": "-1"}]
    body["accounts"] = body["accounts"][1:]
    assert client.post("/api/positions/aggregate", json=body).status_code == 400
    body["events"] = [body["events"][1]]
    remaining = client.post("/api/positions/aggregate", json=body).get_json()
    assert remaining["totals"]["funding"] == [{"currency": "USD", "amount": "-1"}]
    assert remaining["assets"][0]["net_quantity"] == "-0.5"


def test_aggregate_rejects_duplicate_canonical_accounts_before_double_counting(anonymous_app):
    now = datetime.now(timezone.utc).isoformat()
    body = aggregate_body()
    for index, address in enumerate(("0x" + "Ab" * 20, "0x" + "ab" * 20)):
        body["accounts"].append({
            "id": f"aabbccdd-1111-4444-8888-aabbccddeef{index}",
            "venue": "hyperliquid", "address": address, "label": "Account",
            "created_at": now, "snapshot": None, "has_read_only_token": False,
        })
    assert anonymous_app.test_client().post("/api/positions/aggregate", json=body).status_code == 400



@pytest.mark.parametrize("count,status", [(32, 200), (33, 400)])
def test_aggregate_account_limit_remains_32_after_discovery(anonymous_app, count, status):
    body = aggregate_body()
    now = datetime.now(timezone.utc).isoformat()
    for index in range(count):
        body["accounts"].append({
            "id": f"aabbccdd-1111-4444-8888-{index:012x}", "venue": "lighter",
            "address": str(index), "label": "Account", "created_at": now,
            "snapshot": None, "has_read_only_token": False,
        })
    assert anonymous_app.test_client().post("/api/positions/aggregate", json=body).status_code == status

@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e999", "1e-999", "9" * 97, 1, True, {}, []])
def test_untrusted_financial_scalars_rejected_before_arithmetic(anonymous_app, value):
    body = aggregate_body()
    body["fx"] = {"ts": datetime.now(timezone.utc).isoformat(), "source": "Coinbase", "rates": {"USDC": value}}
    response = anonymous_app.test_client().post("/api/positions/aggregate", json=body)
    assert response.status_code == 400
    assert b"Traceback" not in response.data


class PublicLighterTransport:
    """Protocol-level fixture: real adapter parses metadata and account ledgers."""
    def __init__(self, **kwargs):
        self.required_token = None
        self.failure = False
        self.repeat_cursor = False
        self.stall = False
        self.two_stalls = Event()
        self.resume = Event()
        self.active_stalls = 0

    async def aclose(self):
        pass

    async def get_json(self, url, *, params=None, headers=None):
        endpoint = url.rsplit("/", 1)[-1]
        if endpoint == "accountsByL1Address":
            wallet = params["l1_address"]
            if self.failure:
                raise RuntimeError("provider-private-discovery-response")
            return {"code": 200, "l1_address": wallet, "sub_accounts": [
                {"code": 0, "index": index, "l1_address": wallet, "status": 1}
                for index in ([456] if params.get("cursor") else [123])
            ], "next_cursor": "" if params.get("cursor") else "page2"}
        if endpoint == "assetDetails":
            return {"code": 200, "asset_details": [{"asset_id": 3, "symbol": "USDC", "margin_mode": "enabled"}]}
        if endpoint == "orderBookDetails":
            return {"code": 200, "order_book_details": [{"market_id": 4, "symbol": "BTC", "market_type": "perp", "multiplier": "1"}], "spot_order_book_details": []}
        if endpoint == "positionFunding":
            if self.stall:
                import asyncio
                self.active_stalls += 1
                if self.active_stalls == 2:
                    self.two_stalls.set()
                while not self.resume.is_set():
                    await asyncio.sleep(0.01)
            if self.failure:
                raise RuntimeError(f"provider body {self.required_token}")
            if self.required_token is not None:
                assert headers == {"Authorization": self.required_token}
            else:
                assert headers is None
            cursor = params.get("cursor")
            return {"code": 200, "position_fundings": [{
                "timestamp": int(params["end_timestamp"]) - 1, "market_id": 4,
                "funding_id": int(params["account_index"]), "change": "0.123456789",
            }], "next_cursor": "page2" if cursor is None or self.repeat_cursor else ""}
        if endpoint == "account":
            return {"code": 200, "total": 1, "accounts": [{
                "code": 0, "index": int(params["value"]), "account_trading_mode": 0,
                "positions": [{"market_id": 4, "symbol": "BTC", "sign": -1, "position": "2",
                               "position_value": "200", "avg_entry_price": "101", "unrealized_pnl": "2"}],
                "assets": [], "total_asset_value": "100",
            }], "next_cursor": ""}
        raise AssertionError("Unexpected public endpoint")


@pytest.fixture
def live_reader_app(anonymous_app, monkeypatch):
    import web.position_reader as module
    transport = PublicLighterTransport()
    monkeypatch.setattr(module, "HttpClient", lambda **kwargs: transport)
    anonymous_app.extensions["position_reader"] = PositionReader()
    return anonymous_app, transport


def test_generic_api_uses_actual_reader_adapter_discovery_without_persisting_accounts(live_reader_app):
    app, transport = live_reader_app
    client = app.test_client()
    body = {"venue": "lighter", "wallet": "0x" + "A" * 40}
    first = client.post("/api/positions/accounts", json=body)
    assert first.status_code == 200
    data = first.get_json()
    assert data["venue"] == "lighter" and data["wallet"] == "0x" + "a" * 40
    assert [row["address"] for row in data["accounts"]] == ["123"]
    assert data["next_cursor"] == "page2"
    second = client.post("/api/positions/accounts", json={**body, "cursor": data["next_cursor"]})
    assert second.status_code == 200
    assert [row["address"] for row in second.get_json()["accounts"]] == ["456"]
    assert second.get_json()["next_cursor"] is None
    transport.failure = True
    failed = client.post("/api/positions/accounts", json=body)
    assert failed.status_code == 502
    assert b"provider-private-discovery-response" not in failed.data
    assert "accounts" not in failed.get_json()
    for response in (first, second, failed):
        assert response.cache_control.no_store
        assert "Set-Cookie" not in response.headers
    assert client.post("/api/positions/aggregate", json=aggregate_body()).get_json()["accounts"] == []


def test_actual_anonymous_adapter_keeps_account_reads_and_tokens_isolated(live_reader_app):
    app, transport = live_reader_app
    one, two = app.test_client(), app.test_client()
    positions = one.post("/api/positions/read", json={"venue": "lighter", "address": "123", "kind": "positions"})
    assert positions.status_code == 200
    assert positions.get_json()["snapshot"]["positions"][0]["quantity"] == "-2"
    token = "ro:123:single:9999999999:" + "a" * 32
    transport.required_token = token
    body = {"venue": "lighter", "address": "123", "kind": "funding", "hours": 24, "read_only_token": token}
    page = one.post("/api/positions/read", json=body)
    assert page.status_code == 200
    assert page.get_json()["events"][0]["amount"] == "0.123456789"
    assert token.encode() not in page.data
    assert page.get_json()["checked_at"] is None
    body["scan"] = page.get_json()["scan"]
    completed = one.post("/api/positions/read", json=body)
    assert completed.status_code == 200
    assert completed.get_json()["complete"] is True
    assert completed.get_json()["scan"]["since"] == body["scan"]["since"]
    transport.required_token = None
    other = two.post("/api/positions/read", json={"venue": "lighter", "address": "456", "kind": "funding", "hours": 24})
    assert other.status_code == 200
    assert other.get_json()["events"][0]["event_id"] == "4:456"
    assert "Set-Cookie" not in other.headers


def test_provider_error_and_nonprogress_never_become_flat_or_expose_credentials(live_reader_app):
    app, transport = live_reader_app
    client = app.test_client()
    token = "ro:123:single:9999999999:" + "b" * 32
    transport.required_token = token
    body = {"venue": "lighter", "address": "123", "kind": "funding", "read_only_token": token, "hours": 24}
    first = client.post("/api/positions/read", json=body)
    body["scan"] = first.get_json()["scan"]
    transport.repeat_cursor = True
    repeated = client.post("/api/positions/read", json=body)
    assert repeated.status_code == 502
    assert "events" not in repeated.get_json()
    transport.failure = True
    failed = client.post("/api/positions/read", json=body)
    assert failed.status_code == 502
    assert token.encode() not in failed.data and b"provider body" not in failed.data
    assert "Set-Cookie" not in failed.headers


def test_busy_admission_is_bounded_and_recovers_after_concurrent_reads(live_reader_app):
    app, transport = live_reader_app
    transport.stall = True
    body = {"venue": "lighter", "address": "123", "kind": "funding", "hours": 24}
    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = [workers.submit(lambda: app.test_client().post("/api/positions/read", json=body)) for _ in range(2)]
        try:
            assert transport.two_stalls.wait(timeout=5)
            response = app.test_client().post("/api/positions/read", json=body)
            assert response.status_code == 503
            assert "events" not in response.get_json()
        finally:
            transport.stall = False
            transport.resume.set()
        for future in pending:
            assert future.result(timeout=5).get_json()["events"][0]["amount"] == "0.123456789"
    recovered = app.test_client().post("/api/positions/read", json=body)
    assert recovered.get_json()["events"][0]["amount"] == "0.123456789"


def test_deadline_cancels_read_and_releases_admission(live_reader_app):
    app, transport = live_reader_app
    reader = app.extensions["position_reader"]
    reader.deadline = 0.05
    transport.stall = True
    body = {"venue": "lighter", "address": "123", "kind": "funding", "hours": 24}
    response = app.test_client().post("/api/positions/read", json=body)
    assert response.status_code == 502
    assert "events" not in response.get_json()
    transport.stall = False
    assert app.test_client().post("/api/positions/read", json=body).status_code == 200


def test_funding_cursor_cannot_change_fixed_account_or_time_window(live_reader_app):
    app, _transport = live_reader_app
    client = app.test_client()
    body = {"venue": "lighter", "address": "123", "kind": "funding", "hours": 24}
    first = client.post("/api/positions/read", json=body).get_json()
    body["scan"] = first["scan"]
    body["scan"]["cursor"]["since"] = 0
    response = client.post("/api/positions/read", json=body)
    assert response.status_code == 502
    assert "events" not in response.get_json()
    body["scan"] = first["scan"]
    body["scan"]["until"] = datetime.now(timezone.utc).isoformat()
    assert client.post("/api/positions/read", json=body).status_code == 400


def test_finished_old_funding_scan_remains_stale_instead_of_claiming_current_coverage(live_reader_app):
    app, _transport = live_reader_app
    client = app.test_client()
    until = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=20)
    since = until - timedelta(hours=24)
    request_body = {
        "venue": "lighter", "address": "123", "kind": "funding", "hours": 24,
        "scan": {"since": since.isoformat(), "until": until.isoformat(), "cursor": None,
                 "coverage_since": since.isoformat(), "coverage_note": None},
    }
    first = client.post("/api/positions/read", json=request_body)
    assert first.status_code == 200
    request_body["scan"] = first.get_json()["scan"]
    completed = client.post("/api/positions/read", json=request_body)
    assert completed.status_code == 200
    data = completed.get_json()
    account_id = "aabbccdd-1111-4444-8888-aabbccddeeff"
    result = client.post("/api/positions/aggregate", json={
        "hours": 24, "fx": None,
        "accounts": [{"id": account_id, "venue": "lighter", "address": "123", "label": "History",
                      "created_at": since.isoformat(), "snapshot": None, "has_read_only_token": False,
                      "funding_checked_at": data["checked_at"], "funding_since": data["scan"]["coverage_since"]}],
        "events": [{**event, "account_id": account_id} for event in data["events"]],
    })
    assert result.status_code == 200
    payload = result.get_json()
    assert payload["accounts"][0]["funding"]["state"] == "stale"
    assert payload["totals"]["funding_complete"] is False
    assert payload["totals"]["funding"] == [{"currency": "USDC", "amount": "0.123456789"}]
