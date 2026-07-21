"""JSON API — feeds the charts. All endpoints require login.

Decimal -> float happens HERE (the serialization edge), never in the query
layer. Candle payloads use lightweight-charts' native shape:
{time: <unix seconds>, open, high, low, close} plus volume.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, current_app, jsonify, request
from flask_login import login_required

bp = Blueprint("api", __name__, url_prefix="/api")


def _store():
    return current_app.extensions["read_storage"]


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
            "oi_delta_pct": float(r["oi_delta_pct"]) if r["oi_delta_pct"] is not None else None,
            "volume_24h": float(r["volume_24h"]) if r["volume_24h"] is not None else None,
            "mark_price": float(r["mark_price"]) if r["mark_price"] is not None else None,
        })
    return jsonify(out)

@bp.get("/venue-volume")
@login_required
def venue_volume():
    data = _store().venue_volume()
    f = lambda v: float(v) if v is not None else None
    return jsonify({
        "latest": [{"exchange": r["exchange"], "ts": r["ts"].isoformat(),
                    "total": f(r["volume_total"]), "spot": f(r["volume_spot"]),
                    "perp": f(r["volume_perp"])} for r in data["latest"]],
        "series": [{"ts": r["ts"].isoformat(), "cex_share_pct": f(r["cex_share_pct"]),
                    "total": f(r["total"])} for r in data["series"]],
    })

@bp.get("/fills-pulse")
@login_required
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
@login_required
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


@bp.get("/freshness")
@login_required
def freshness():
    """Pipeline liveness for the stale-data banner: age of the newest bar
    anywhere. null age = empty database (also worth a banner)."""
    row = _store().ingest_freshness()
    if row is None:
        return jsonify(age_seconds=None, last_ts=None)
    return jsonify(age_seconds=float(row["age_seconds"]),
                   last_ts=row["last_ts"].isoformat())


@bp.get("/wallet-share")
@login_required
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
@login_required
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