from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import pytest
from flask import Response, request, url_for

from web import create_app
from web.cache import ResponseCache
from web.views.api import _candle_bucket


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


def test_health_probes_every_configured_series_and_flags_ones_never_stored(client, monkeypatch) -> None:
    probed = []

    def freshness(series):
        probed.extend(series)
        live = {"exchange": "binance", "market_type": "perp", "symbol": "BTC/USDT", "interval": "1m",
                "last_ts": datetime.now(timezone.utc), "age": timedelta(seconds=30), "stale_factor": 0.5}
        return [{**live, "exchange": "phoenix", "symbol": "BTC", "last_ts": None, "age": None,
                 "stale_factor": None}, live]

    monkeypatch.setattr(client.application.extensions["read_storage"], "freshness", freshness)
    client.post("/login", data={"email": "mock@overseer.local", "password": "mock"})
    page = client.get("/health")
    assert page.status_code == 200
    # Keyed as the scheduler stores bars, HIP-3 markets under the hyperliquid exchange.
    assert {("binance", "perp", "BTC/USDT", "1m"), ("hyperliquid", "perp", "xyz:GOLD", "1m")} <= set(probed)
    assert b"None stored" in page.data and b"1 / 2 series stale" in page.data


def test_long_candle_windows_are_merged_into_larger_bars_not_cut_short() -> None:
    # A week of 1-minute bars is 10,080: at the charts' 5,000 they become 5-minute bars.
    assert _candle_bucket("1m", 48, 5000) is None
    assert _candle_bucket("1m", 168, 5000) == (300, "5m")
    assert _candle_bucket("1m", 720, 5000) == (900, "15m")
    assert _candle_bucket("1h", 720, 5000) is None
    with pytest.raises(ValueError):
        _candle_bucket("minute", 48, 5000)


def test_oi_history_widens_bars_too_fine_for_the_window_instead_of_dropping_venues(client) -> None:
    month = client.get("/api/liquidity-history-multi?asset=BTC&hours=720&bucket=5").get_json()
    week = client.get("/api/liquidity-history-multi?asset=BTC&hours=168&bucket=5").get_json()
    assert len(month) == len(week) > 3
    # 5-minute bars over 30 days would be 8,640 a venue; they widen to 15 minutes.
    assert {len(points) for points in month.values()} == {30 * 24 * 4}
    assert {len(points) for points in week.values()} == {7 * 24 * 12}


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


def test_no_cache_request_refreshes_and_replaces_cached_response(client) -> None:
    path = "/api/candles?exchange=binance&market_type=perp&symbol=BTC/USDT&hours=24"
    first = client.get(path)
    cached = client.get(path)
    refreshed = client.get(path, headers={"Cache-Control": "no-cache"})
    recached = client.get(path)

    assert first.headers["X-Overseer-Cache"] == "MISS"
    assert cached.headers["X-Overseer-Cache"] == "HIT"
    assert refreshed.headers["X-Overseer-Cache"] == "MISS"
    assert recached.headers["X-Overseer-Cache"] == "HIT"
    assert recached.data == refreshed.data


def test_volume_oi_ratio_returns_weighted_exchange_series(client) -> None:
    response = client.get("/api/volume-oi-ratio?hours=24")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["hours"] == 24
    assert payload["bucket_minutes"] == 15
    assert payload["series"]
    assert len({item["exchange"] for item in payload["series"]}) == len(payload["series"])

    point = payload["series"][0]["points"][0]
    assert point["volume_24h"] >= 0
    assert point["oi_notional"] > 0
    assert point["ratio"] == pytest.approx(
        point["volume_24h"] / point["oi_notional"]
    )


def test_funding_api_exposes_rwa_taxonomy_and_deployer_identity(client) -> None:
    response = client.get("/api/funding")

    assert response.status_code == 200
    payload = response.get_json()
    registry = client.application.extensions["symbols"]
    assert {asset for asset, rows in payload.items()
            if rows[0]["asset_family"] == "traditional"} == {
        asset for asset in registry.assets()
        if registry.metadata(asset).family == "traditional"
    }
    xau = payload["XAU"]
    assert {row["asset_class"] for row in xau} == {"commodity"}
    assert {row["asset_group"] for row in xau} == {"metals"}
    assert all(row["exchange"] != "bulk" for row in xau)
    assert any(
        row["venue_key"] == "hyperliquid:xyz"
        and row["deployer"] == "xyz"
        for row in xau
    )

    history = client.get(
        "/api/funding-history-multi?asset=XAU&hours=24"
    ).get_json()
    assert "hyperliquid:xyz" in history
    history_point = history["hyperliquid:xyz"][0]
    assert history_point["interval_hours"] > 0
    assert history_point["apr_pct"] == pytest.approx(
        history_point["rate_pct"] * 8760 / history_point["interval_hours"],
        abs=1e-4,
    )


def test_funding_api_merges_quote_denominated_symbols(client) -> None:
    payload = client.get("/api/funding").get_json()

    assert "WIF" in payload
    assert "WIF/USDT" not in payload
    assert {
        (row["exchange"], row["symbol"])
        for row in payload["WIF"]
    } >= {
        ("lighter", "WIF"),
        ("bybit", "WIF/USDT"),
    }

    assert "PUMP" in payload
    assert "PUMP-USD" not in payload
    assert {
        (row["exchange"], row["symbol"])
        for row in payload["PUMP"]
    } >= {
        ("hyperliquid", "PUMP"),
        ("extended", "PUMP-USD"),
    }

    history = client.get(
        "/api/funding-history-multi?asset=WIF&hours=24"
    ).get_json()
    assert {"lighter", "bybit"} <= set(history)
    assert {
        point["symbol"]
        for series in history.values()
        for point in series
    } >= {"WIF", "WIF/USDT"}

    liquidity = client.get(
        "/api/liquidity-history-multi?asset=WIF&hours=24"
    ).get_json()
    assert {"lighter", "bybit"} <= set(liquidity)


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


def test_static_urls_change_when_their_file_does(tmp_path, monkeypatch) -> None:
    # Static URLs are cached for a year, so a changed file must get a new URL:
    # a reused tag kept browsers on stale funding page code once.
    monkeypatch.setenv("OVERSEER_MOCK", "1")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    app = create_app()
    app.static_folder = str(tmp_path)
    asset = tmp_path / "app.css"
    asset.write_text("a{}")
    with app.test_request_context():
        first = url_for("static", filename="app.css")
        assert url_for("static", filename="app.css") == first
        asset.write_text("a{color:red}")
        stat = asset.stat()
        os.utime(asset, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        changed = url_for("static", filename="app.css")
    assert "?v=" in first
    assert changed != first


def test_every_page_asset_url_carries_a_version(client) -> None:
    html = client.get("/funding").get_data(as_text=True)
    assets = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert assets
    assert all("?v=" in url for url in assets), assets
