from __future__ import annotations

import pytest
from flask import Response, request

from web import create_app
from web.cache import ResponseCache


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OVERSEER_MOCK", "1")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.delenv("OVERSEER_PROXY_HOPS", raising=False)
    monkeypatch.delenv("OVERSEER_SECURE_COOKIES", raising=False)
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_analytics_are_public_but_health_remains_internal(client) -> None:
    for path in ("/", "/basis", "/funding", "/flow", "/wallets"):
        response = client.get(path)
        assert response.status_code == 200
        assert b"OVERSEER" in response.data
        assert b'href="/login"' not in response.data
        assert b'href="/health"' not in response.data
        assert b"internal" not in response.data.lower()

    assert client.get("/api/series").status_code == 200
    health = client.get("/health")
    assert health.status_code == 302
    assert health.headers["Location"].startswith("/login?next=")



def test_authorized_user_sees_health_navigation(client) -> None:
    login = client.post(
        "/login",
        data={"email": "mock@overseer.local", "password": "mock"},
    )
    assert login.status_code == 302

    response = client.get("/")
    assert response.status_code == 200
    assert b'href="/health"' in response.data
    assert b"internal" not in response.data.lower()


def test_public_api_responses_are_cached_by_full_query(client) -> None:
    first = client.get("/api/candles?exchange=binance&market_type=perp&symbol=BTC/USDT&hours=24")
    repeated = client.get("/api/candles?exchange=binance&market_type=perp&symbol=BTC/USDT&hours=24")
    different_query = client.get(
        "/api/candles?exchange=binance&market_type=perp&symbol=BTC/USDT&hours=48"
    )

    assert first.status_code == repeated.status_code == different_query.status_code == 200
    assert first.headers["X-Overseer-Cache"] == "MISS"
    assert repeated.headers["X-Overseer-Cache"] == "HIT"
    assert different_query.headers["X-Overseer-Cache"] == "MISS"
    assert repeated.data == first.data
    assert first.headers["Cache-Control"] == "public, max-age=30, stale-while-revalidate=30"


def test_orderbook_spread_api_returns_bucketed_executable_quotes(client) -> None:
    response = client.get("/api/orderbook-spreads?asset=BTC&market=perp&hours=24")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["asset"] == "BTC"
    assert payload["market"] == "perp"
    assert payload["bucket_seconds"] == 60
    assert payload["series"]

    point = payload["series"][0]["points"][0]
    assert point["bid_price"] < point["ask_price"]
    assert point["bid_size"] > 0
    assert point["ask_size"] > 0
    assert point["spread_bps"] > 0


def test_proxy_deployment_trusts_one_hop_and_secures_session(monkeypatch) -> None:
    monkeypatch.setenv("OVERSEER_MOCK", "1")
    monkeypatch.setenv("FLASK_SECRET_KEY", "proxy-test-secret")
    monkeypatch.setenv("OVERSEER_PROXY_HOPS", "1")
    monkeypatch.setenv("OVERSEER_SECURE_COOKIES", "true")
    app = create_app()
    app.config["TESTING"] = True

    @app.get("/_proxy-test")
    def proxy_test():
        return f"{request.scheme}|{request.remote_addr}"

    proxy_client = app.test_client()
    forwarded = proxy_client.get(
        "/_proxy-test",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "203.0.113.7",
        },
    )
    assert forwarded.data == b"https|203.0.113.7"

    login = proxy_client.post(
        "/login",
        data={"email": "mock@overseer.local", "password": "mock"},
    )
    cookie = login.headers["Set-Cookie"]
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_response_cache_is_size_bounded_and_never_replays_cookies() -> None:
    bounded = ResponseCache(ttl_seconds=30, max_entries=2, max_bytes=4)
    oversized = bounded.put("large", Response(b"12345"))
    assert oversized.headers["X-Overseer-Cache"] == "BYPASS"
    assert bounded.get("large") is None

    cache = ResponseCache()
    original = Response(b"public")
    original.set_cookie("session", "private")
    cache.put("public", original)
    cached = cache.get("public")
    assert cached is not None
    assert "Set-Cookie" not in cached.headers
