"""Public JSON API feeding the analytics pages.

Decimal -> float happens HERE (the serialization edge), never in the query
layer. Candle payloads use lightweight-charts' native shape:
{time: <unix seconds>, open, high, low, close} plus volume.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, current_app, jsonify, request
from core.depth import HEATMAP_WINDOWS, WINDOW_BPS
from core.enums import Exchange, MarketType
from core.execution_fees import FEE_SCHEDULES, execution_listings, selected_tiers
from core.execution_history import (
    HISTORY_ASSETS, HISTORY_INTERVAL_SECONDS, HISTORY_SIDES, HISTORY_WINDOWS_DAYS, ROUND_TRIP, summarise,
)
from core.positions import asset_id
from data_collection.exchanges.registry import REGISTRY
from web.volatility import rolling_volatility

bp = Blueprint("api", __name__, url_prefix="/api")


def _force_refresh() -> bool:
    """A no-cache request must reach storage, then replace the cached response."""
    return request.cache_control.no_cache


@bp.before_request
def _serve_cached_response() -> Response | None:
    if request.method != "GET" or _force_refresh():
        return None
    key = f"{request.endpoint}:{request.full_path}"
    return current_app.extensions["response_cache"].get(key)


@bp.after_request
def _cache_successful_response(response: Response) -> Response:
    if request.method != "GET" or response.headers.get("X-Overseer-Cache") == "HIT":
        return response
    key = f"{request.endpoint}:{request.full_path}"
    return current_app.extensions["response_cache"].put(key, response)


def _store():
    return current_app.extensions["read_storage"]


def _float_or_none(value):
    return float(value) if value is not None else None




def _funding_symbols_for_asset(store, registry, requested_asset: str) -> list[str]:
    """Resolve every current venue symbol represented by one funding asset."""
    if not requested_asset:
        return []
    asset = asset_id(registry, requested_asset)
    symbols = {requested_asset, asset, *registry.listings(asset).values()}
    symbols.update(
        row["symbol"]
        for row in store.funding_table()
        if asset_id(registry, row["symbol"]) == asset
    )
    symbols.discard("")
    return sorted(symbols)


def _maybe_csv(rows: list[dict], filename: str) -> Response | None:
    """?format=csv turns a list-of-dicts payload into a CSV download; None
    means the caller should jsonify as usual."""
    if request.args.get("format") != "csv":
        return None
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    safe = filename.replace("/", "-")
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={safe}.csv"},
    )


@bp.get("/series")
def series():
    rows = _store().series_list()
    return jsonify([
        {
            "exchange": r["exchange"], "market_type": r["market_type"],
            "symbol": r["symbol"], "interval": r["interval"],
            "bars": r["bars"],
            "first_ts": r["first_ts"].isoformat(), "last_ts": r["last_ts"].isoformat(),
        }
        for r in rows
    ])


@bp.get("/candles")
def candles():
    q = request.args
    try:
        exchange = q["exchange"]
        market_type = q["market_type"]
        symbol = q["symbol"]
        interval = q.get("interval", "1m")
    except KeyError as missing:
        return jsonify(error=f"missing query param: {missing}"), 400
    limit = min(int(q.get("limit", 1000)), 5000)
    hours = float(q.get("hours", 48))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    rows = _store().candles(exchange, market_type, symbol, interval,
                            since=since, limit=limit)
    payload = [
        {
            "time": int(r["ts"].timestamp()),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": float(r["volume"]),
        }
        for r in rows
    ]
    return _maybe_csv(payload, f"candles_{exchange}_{symbol}_{interval}") \
        or jsonify(payload)


@bp.get("/realised-volatility")
def realised_volatility():
    q = request.args
    try:
        exchange, market_type, symbol = q["exchange"], q["market_type"], q["symbol"]
        hours = int(q.get("hours", 48))
        window_hours = int(q.get("window_hours", 24))
    except KeyError as missing:
        return jsonify(error=f"missing query param: {missing}"), 400
    except ValueError:
        return jsonify(error="hours and window_hours must be integers"), 400
    if not 1 <= hours <= 168 or window_hours not in (1, 6, 24):
        return jsonify(error="hours must be 1–168; window_hours must be 1, 6, or 24"), 400
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    # Include both closes at the beginning of the first rolling window.
    since = end - timedelta(hours=hours + window_hours, minutes=2)
    rows = _store().candles(
        exchange, market_type, symbol, "1m", since=since,
        until=end - timedelta(minutes=1), limit=(hours + window_hours) * 60 + 2,
    )
    return jsonify(rolling_volatility(
        rows, end=int(end.timestamp()), hours=hours, window_hours=window_hours,
    ))


@bp.get("/orderbook-spreads")
def orderbook_spreads():
    q = request.args
    asset = q.get("asset", "").strip().upper()
    market = q.get("market", "perp")
    registry = current_app.extensions["symbols"]
    if asset not in registry.assets():
        return jsonify(error="unknown asset"), 400
    if market not in {MarketType.SPOT.value, MarketType.PERP.value}:
        return jsonify(error="market must be 'spot' or 'perp'"), 400
    try:
        hours = max(1.0, min(float(q.get("hours", 48)), 720.0))
    except ValueError:
        return jsonify(error="hours must be numeric"), 400

    known_exchanges = {exchange.value for exchange in Exchange}
    requested = {
        value.strip()
        for value in q.get("exchanges", "").split(",")
        if value.strip()
    }
    invalid = requested - known_exchanges
    if invalid:
        return jsonify(error=f"unknown exchanges: {', '.join(sorted(invalid))}"), 400
    exchanges = sorted(requested or known_exchanges)
    symbols = sorted(set(registry.listings(asset).values()))
    bucket_minutes = 1 if hours <= 48 else 5 if hours <= 168 else 15
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = _store().orderbook_spreads(
        market, symbols, exchanges, since, bucket_minutes
    )

    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (row["exchange"], row["symbol"])
        grouped.setdefault(key, []).append({
            "time": int(row["ts"].timestamp()),
            "spread_bps": float(row["spread_bps"]),
            "midpoint": float(row["midpoint"]),
            "bid_price": float(row["bid_price"]),
            "ask_price": float(row["ask_price"]),
            "bid_size": float(row["bid_size"]),
            "ask_size": float(row["ask_size"]),
        })
    return jsonify({
        "asset": asset,
        "market": market,
        "bucket_seconds": bucket_minutes * 60,
        "series": [
            {"exchange": exchange, "symbol": symbol, "points": points}
            for (exchange, symbol), points in grouped.items()
        ],
    })


@bp.get("/basis")
def basis():
    q = request.args
    try:
        spot_exchange = q["spot_exchange"]
        spot_symbol = q["spot_symbol"]
        perp_exchange = q["perp_exchange"]
        perp_symbol = q["perp_symbol"]
    except KeyError as missing:
        return jsonify(error=f"missing query param: {missing}"), 400
    interval = q.get("interval", "1m")
    limit = min(int(q.get("limit", 2000)), 10000)
    hours = float(q.get("hours", 48))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    rows = _store().basis(spot_exchange, spot_symbol, perp_exchange, perp_symbol,
                          interval, since=since, limit=limit)
    return jsonify([
        {
            "time": int(r["ts"].timestamp()),
            "spot": float(r["spot_close"]), "perp": float(r["perp_close"]),
            "basis": float(r["basis"]), "basis_pct": float(r["basis_pct"]),
        }
        for r in rows
    ])

@bp.get("/funding")
def funding():
    """Latest annualized funding + liquidity per (venue, perp), grouped by
    configured asset id and then by unquoted base asset. This keeps equivalent
    venue symbols such as DOGE, DOGE/USDT, and DOGE-USD on one funding row."""
    registry = current_app.extensions["symbols"]
    rows = _store().funding_table()
    out: dict[str, list] = {}
    for r in rows:
        asset = asset_id(registry, r["symbol"])
        metadata = registry.metadata(asset)
        venue = registry.venue_identity(r["exchange"], r["symbol"])
        out.setdefault(asset, []).append({
            "exchange": r["exchange"], "symbol": r["symbol"],
            "venue_key": venue.key,
            "venue_label": venue.label,
            "deployer": venue.deployer,
            "asset_family": metadata.family,
            "asset_class": metadata.asset_class,
            "asset_group": metadata.group,
            "asset_label": metadata.label,
            "rate": float(r["rate"]),
            "interval_hours": r["interval_hours"],
            "apr_pct": float(r["apr_pct"]),
            "rate_kind": r.get("rate_kind", "settled"),
            "rate_ts": (r.get("rate_ts") or r["funding_ts"]).isoformat(),
            "funding_ts": (
                r["funding_ts"].isoformat() if r.get("funding_ts") is not None else None
            ),
            "settled_rate": (
                float(r["settled_rate"]) if r.get("settled_rate") is not None else None
            ),
            "settled_interval_hours": r.get("settled_interval_hours"),
            "settled_apr_pct": (
                float(r["settled_apr_pct"])
                if r.get("settled_apr_pct") is not None else None
            ),
            "current_funding_rate": (
                float(r["current_funding_rate"])
                if r.get("current_funding_rate") is not None else None
            ),
            "current_interval_hours": r.get("current_interval_hours"),
            "next_funding_at": (
                r["next_funding_at"].isoformat()
                if r.get("next_funding_at") is not None else None
            ),
            "funding_premium": (
                float(r["funding_premium"])
                if r.get("funding_premium") is not None else None
            ),
            "index_price": (
                float(r["index_price"]) if r.get("index_price") is not None else None
            ),
            "oi": float(r["open_interest"]) if r["open_interest"] is not None else None,
            "oi_notional": float(r["oi_notional"]) if r["oi_notional"] is not None else None,
            "oi_delta_pct": float(r["oi_delta_pct"]) if r["oi_delta_pct"] is not None else None,
            "volume_24h": float(r["volume_24h"]) if r["volume_24h"] is not None else None,
            "mark_price": float(r["mark_price"]) if r["mark_price"] is not None else None,
        })
    return jsonify(out)

@bp.get("/venue-volume")
def venue_volume():
    data = _store().venue_volume()
    f = _float_or_none
    return jsonify({
        "latest": [{"exchange": r["exchange"], "ts": r["ts"].isoformat(),
                    "total": f(r["volume_total"]), "spot": f(r["volume_spot"]),
                    "perp": f(r["volume_perp"])} for r in data["latest"]],
        "series": [{"ts": r["ts"].isoformat(), "cex_share_pct": f(r["cex_share_pct"]),
                    "total": f(r["total"])} for r in data["series"]],
    })

@bp.get("/fills-pulse")
def fills_pulse():
    labels = current_app.extensions.get("fills_labels", {})
    rows = _store().fills_pulse()
    return jsonify([
        {"label": labels.get(r["wallet_address"], r["wallet_address"][:10] + "…"),
         "fills_24h": r["fills_24h"], "buys": r["buys"], "sells": r["sells"],
         "last_ts": r["last_ts"].isoformat()}
        for r in rows
    ])

@bp.get("/funding-history")
def funding_history():
    exchange = request.args.get("exchange", "")
    symbol = request.args.get("symbol", "")
    hours = min(int(request.args.get("hours", 48)), 24 * 30)
    rows = _store().funding_series(exchange, symbol, hours=hours)
    payload = [
        {"time": int(r["ts"].timestamp()), "rate": float(r["rate"]),
         "interval_hours": r["interval_hours"], "apr_pct": float(r["apr_pct"])}
        for r in rows
    ]
    return _maybe_csv(payload, f"funding_{exchange}_{symbol}") or jsonify(payload)


@bp.get("/funding-history-multi")
def funding_history_multi():
    """Settled funding history for one aggregated base asset, grouped by its
    display venue. HIP-3 deployers stay distinct from native Hyperliquid."""
    requested_asset = request.args.get("asset", "")
    hours = min(int(request.args.get("hours", 48)), 24 * 30)
    registry = current_app.extensions["symbols"]
    store = _store()
    native_symbols = _funding_symbols_for_asset(store, registry, requested_asset)
    rows = store.funding_series_multi(native_symbols, hours=hours)
    out: dict[str, list] = {}
    for r in rows:
        venue = registry.venue_identity(r["exchange"], r["symbol"])
        out.setdefault(venue.key, []).append({
            "time": int(r["ts"].timestamp()),
            "rate_pct": float(r["rate"]) * 100,
            "interval_hours": r["interval_hours"],
            "apr_pct": float(r["apr_pct"]),
            "exchange": r["exchange"],
            "symbol": r["symbol"],
            "venue_label": venue.label,
            "deployer": venue.deployer,
        })
    return jsonify(out)


@bp.get("/liquidity-history-multi")
def liquidity_history_multi():
    """OI-notional history for one aggregated base asset, grouped by its
    display venue. HIP-3 deployers stay distinct from native Hyperliquid."""
    requested_asset = request.args.get("asset", "")
    hours = min(int(request.args.get("hours", 48)), 24 * 30)
    bucket_arg = request.args.get("bucket")
    bucket_minutes = min(max(int(bucket_arg), 1), 1440) if bucket_arg else None
    registry = current_app.extensions["symbols"]
    store = _store()
    native_symbols = _funding_symbols_for_asset(store, registry, requested_asset)
    rows = store.liquidity_series_multi(
        native_symbols, hours=hours, bucket_minutes=bucket_minutes
    )
    out: dict[str, list] = {}
    for r in rows:
        venue = registry.venue_identity(r["exchange"], r["symbol"])
        out.setdefault(venue.key, []).append({
            "time": int(r["ts"].timestamp()),
            "oi_notional": float(r["oi_notional"]),
            "exchange": r["exchange"],
            "symbol": r["symbol"],
            "venue_label": venue.label,
            "deployer": venue.deployer,
        })
    return jsonify(out)

@bp.get("/volume-oi-ratio")
def volume_oi_ratio():
    """Tracked perpetual-market turnover: reported 24h volume / OI notional."""
    hours = min(max(int(request.args.get("hours", 48)), 1), 24 * 30)
    bucket_arg = request.args.get("bucket")
    bucket_minutes = (
        min(max(int(bucket_arg), 1), 1440)
        if bucket_arg
        else (15 if hours <= 48 else 60 if hours <= 168 else 240)
    )
    rows = _store().volume_oi_ratio(hours=hours, bucket_minutes=bucket_minutes)
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row["exchange"], []).append({
            "time": int(row["ts"].timestamp()),
            "volume_24h": float(row["volume_24h"]),
            "oi_notional": float(row["oi_notional"]),
            "ratio": float(row["ratio"]),
        })
    return jsonify({
        "hours": hours,
        "bucket_minutes": bucket_minutes,
        "series": [
            {"exchange": exchange, "points": points}
            for exchange, points in grouped.items()
        ],
    })



@bp.get("/trade-flow")
def trade_flow():
    """Per-minute order flow for one canonical asset, per venue, plus the
    funding settlement times inside the window.

    The settlements travel with the payload because they are the reason this
    page exists: flow is only interesting here relative to when funding pays.
    They are derived, not stored — settlement times are latest funding ts plus
    whole multiples of that market's interval_hours, which is why the interval
    rides on every funding row.
    """
    asset = request.args.get("asset", "")
    hours = min(int(request.args.get("hours", 6)), 24 * 7)
    bucket_arg = request.args.get("bucket")
    bucket_minutes = min(max(int(bucket_arg), 1), 1440) if bucket_arg else None

    registry = current_app.extensions["symbols"]
    native_symbols = list(set(registry.listings(asset).values()))
    store = _store()
    rows = store.trade_flow_multi(
        native_symbols, hours=hours, bucket_minutes=bucket_minutes)

    f = _float_or_none
    series: dict[str, list] = {}
    for r in rows:
        series.setdefault(r["exchange"], []).append({
            "time": int(r["ts"].timestamp()),
            "volume": f(r["volume"]),
            "notional": f(r["notional"]),
            "trades": int(r["trades"]),
            "buy_volume": f(r["buy_volume"]),
            "sell_volume": f(r["sell_volume"]),
            "imbalance_pct": f(r["imbalance_pct"]),
            "large_trades": int(r["large_trades"] or 0),
            "large_imbalance_pct": f(r["large_imbalance_pct"]),
            "max_trade_notional": f(r["max_trade_notional"]),
        })

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    settlements: dict[str, list[int]] = {}
    for row in store.funding_table():
        if row["symbol"] not in native_symbols:
            continue
        anchor = row.get("funding_ts") or row.get("next_funding_at")
        if anchor is None:
            continue
        step = timedelta(hours=row["interval_hours"])
        # walk back from the newest settlement to the window start, then
        # forward again — covers the case where funding polling lags.
        t = anchor
        while t > start:
            t -= step
        marks = settlements.setdefault(row["exchange"], [])
        while t <= now:
            if t >= start:
                marks.append(int(t.timestamp()))
            t += step

    return jsonify({
        "asset": asset,
        "series": series,
        "settlements": {k: sorted(set(v)) for k, v in settlements.items()},
    })


@bp.get("/freshness")
def freshness():
    """Pipeline liveness for the stale-data banner: age of the newest bar
    anywhere. null age = empty database (also worth a banner)."""
    row = _store().ingest_freshness()
    if row is None:
        return jsonify(age_seconds=None, last_ts=None)
    return jsonify(age_seconds=float(row["age_seconds"]),
                   last_ts=row["last_ts"].isoformat())


@bp.get("/wallet-share")
def wallet_share():
    """Each tracked wallet's 24h fill notional vs its venue's 24h volume
    (the venue_volume daily sweep — the WHOLE venue, not just tracked symbols)
    — feeds the share meters on the wallets page."""
    wallets = current_app.extensions.get("tracked_wallets", [])
    if not wallets:
        return jsonify([])
    store = _store()
    wal = {r["wallet_address"]: r
           for r in store.wallet_volume_24h([w["address"] for w in wallets])}
    ven = {ex: float(v) for ex, v in
           store.venue_volume_latest([w["venue"] for w in wallets]).items()}
    out = []
    for w in wallets:
        row = wal.get(w["address"])
        wallet_notional = float(row["notional"]) if row else 0.0
        venue_notional = ven.get(w["venue"])
        out.append({
            "label": w["label"], "venue": w["venue"],
            "fills_24h": row["fills"] if row else 0,
            "wallet_notional_24h": wallet_notional,
            "venue_notional_24h": venue_notional,
            "share_pct": (wallet_notional / venue_notional * 100)
                         if venue_notional else None,
        })
    return jsonify(out)


@bp.get("/wallet-flows")
def wallet_flows():
    """Per-wallet 5m net flow for one symbol, keyed by wallet label — the
    wallets page cumulates into position-drift lines."""
    symbol = request.args.get("symbol", "")
    hours = min(float(request.args.get("hours", 48)), 24 * 14)
    wallets = current_app.extensions.get("tracked_wallets", [])
    labels = {w["address"]: w["label"] for w in wallets}
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = _store().wallet_flows(symbol, list(labels), since) if wallets else []
    out: dict[str, list] = {label: [] for label in labels.values()}
    for r in rows:
        label = labels.get(r["wallet_address"], r["wallet_address"])
        out.setdefault(label, []).append(
            {"time": int(r["bucket"].timestamp()), "net": float(r["net"])})
    return jsonify(out)


@bp.get("/execution-history")
def execution_history():
    """Cheapest-venue statistics from scheduled captures. Fees are applied
    here: base tiers by default, or ?fees=venue:tier,… for chosen tiers."""
    q = request.args
    asset = q.get("asset", "")
    if asset not in HISTORY_ASSETS:
        return jsonify(error=f"History is collected for {', '.join(HISTORY_ASSETS)}."), 400
    side = q.get("side", "")
    if side not in HISTORY_SIDES:
        return jsonify(error=f"side must be {', '.join(HISTORY_SIDES[:-1])} or {HISTORY_SIDES[-1]}"), 400
    days = q.get("days", "7")
    if not days.isdigit() or int(days) not in HISTORY_WINDOWS_DAYS:
        windows = " or ".join(str(window) for window in HISTORY_WINDOWS_DAYS)
        return jsonify(error=f"days must be {windows}"), 400
    days = int(days)
    requested = dict(pair.partition(":")[::2] for pair in q.get("fees", "").split(",") if pair)
    try:
        tiers = selected_tiers(requested)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    fees = {venue: float(tier["taker_bps"]) if tier["taker_bps"] is not None else None
            for venue, tier in tiers.items()}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = _store().execution_samples(asset, ["buy", "sell"] if side == ROUND_TRIP else [side], since)
    registry = current_app.extensions["symbols"]
    listings = execution_listings(registry, asset)
    sizes = summarise(rows, side, fees)
    for size in sizes:
        for entry in size["venues"]:
            venue, symbol = entry["venue"], listings.get(entry["venue"])
            entry["label"] = FEE_SCHEDULES[venue]["label"] if venue in FEE_SCHEDULES else venue
            entry["identity"] = (registry.venue_identity(REGISTRY[venue].exchange, symbol).key
                                 if symbol and venue in REGISTRY else venue)
    captured = [row["ts"] for row in rows]
    return jsonify(
        asset=asset, side=side, days=days, interval_seconds=HISTORY_INTERVAL_SECONDS,
        first_capture=min(captured).isoformat() if captured else None,
        last_capture=max(captured).isoformat() if captured else None,
        custom_fees=any(tiers[venue]["id"] != FEE_SCHEDULES[venue]["default_tier"]
                        for venue in requested),
        sizes=sizes,
    )


def _compact_sizes(sizes) -> list[float | int]:
    """Four significant figures, far below a heatmap's resolution, and no
    trailing empty buckets: a short array means zero beyond its end."""
    values = [0 if size == 0 else float(f"{size:.4g}") for size in sizes]
    while values and values[-1] == 0:
        values.pop()
    return values


@bp.get("/depth-heatmap")
def depth_heatmap():
    """Stored order-book depth near the mid for one coin on one venue, one
    real snapshot per column; longer windows use longer columns."""
    q = request.args
    asset = q.get("asset", "")
    if asset not in HISTORY_ASSETS:
        return jsonify(error=f"Depth is stored for {', '.join(HISTORY_ASSETS)}."), 400
    registry = current_app.extensions["symbols"]
    listings = execution_listings(registry, asset)
    venue = q.get("venue", "")
    if venue not in listings:
        return jsonify(error=f"venue must be one of {', '.join(listings)}"), 400
    steps = {hours: step for hours, step, _label in HEATMAP_WINDOWS}
    hours = q.get("hours", "24")
    if not hours.isdigit() or int(hours) not in steps:
        return jsonify(error=f"hours must be {', '.join(str(window) for window in steps)}"), 400
    hours = int(hours)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = _store().depth_snapshots(asset, venue, since, steps[hours])
    schedule = FEE_SCHEDULES[venue]
    return jsonify(
        asset=asset, venue=venue, label=schedule["label"],
        identity=registry.venue_identity(REGISTRY[venue].exchange, listings[venue]).key,
        hours=hours, step_seconds=steps[hours], window_bps=WINDOW_BPS,
        columns=[{
            "time": int(row["slot"].timestamp()), "captured_at": row["ts"].isoformat(),
            "source": row["source"], "bucket_bps": row["bucket_bps"], "mid": row["mid"],
            "quote_currency": row["quote_currency"], "usd_rate": row["usd_rate"],
            "book_age_seconds": row["book_age_seconds"],
            "bid_reach_bps": row["bid_reach_bps"], "ask_reach_bps": row["ask_reach_bps"],
            "bid_levels": row["bid_levels"], "ask_levels": row["ask_levels"],
            "bids": _compact_sizes(row["bids"]), "asks": _compact_sizes(row["asks"]),
        } for row in rows],
    )