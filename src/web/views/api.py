"""JSON API — feeds the charts. All endpoints require login.

Decimal -> float happens HERE (the serialization edge), never in the query
layer. Candle payloads use lightweight-charts' native shape:
{time: <unix seconds>, open, high, low, close} plus volume.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, request
from flask_login import login_required

bp = Blueprint("api", __name__, url_prefix="/api")


def _store():
    return current_app.extensions["read_storage"]


@bp.get("/series")
@login_required
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
@login_required
def candles():
    q = request.args
    try:
        exchange = q["exchange"]; market_type = q["market_type"]
        symbol = q["symbol"]; interval = q.get("interval", "1m")
    except KeyError as missing:
        return jsonify(error=f"missing query param: {missing}"), 400
    limit = min(int(q.get("limit", 1000)), 5000)
    hours = float(q.get("hours", 48))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    rows = _store().candles(exchange, market_type, symbol, interval,
                            since=since, limit=limit)
    return jsonify([
        {
            "time": int(r["ts"].timestamp()),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": float(r["volume"]),
        }
        for r in rows
    ])


@bp.get("/basis")
@login_required
def basis():
    q = request.args
    try:
        spot_exchange = q["spot_exchange"]; spot_symbol = q["spot_symbol"]
        perp_exchange = q["perp_exchange"]; perp_symbol = q["perp_symbol"]
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
@login_required
def funding():
    """Latest annualized funding + liquidity per (venue, perp), grouped by the
    canonical asset id from the symbols registry. Rows whose symbol isn't in
    the registry (e.g. a coin removed from config but still in the DB) group
    under their raw symbol rather than being dropped."""
    registry = current_app.extensions["symbols"]
    rows = _store().funding_table()
    out: dict[str, list] = {}
    for r in rows:
        asset = registry.asset_for(r["symbol"]) or r["symbol"]
        out.setdefault(asset, []).append({
            "exchange": r["exchange"], "symbol": r["symbol"],
            "rate": float(r["rate"]),
            "interval_hours": r["interval_hours"],
            "apr_pct": float(r["apr_pct"]),
            "funding_ts": r["funding_ts"].isoformat(),
            "oi": float(r["open_interest"]) if r["open_interest"] is not None else None,
            "oi_notional": float(r["oi_notional"]) if r["oi_notional"] is not None else None,
            "volume_24h": float(r["volume_24h"]) if r["volume_24h"] is not None else None,
            "mark_price": float(r["mark_price"]) if r["mark_price"] is not None else None,
        })
    return jsonify(out)