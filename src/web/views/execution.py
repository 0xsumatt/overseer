"""On-demand, public, read-only perpetual execution estimates."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from flask import Blueprint, current_app, jsonify, request

from core.enums import Side
from core.execution import NOTIONALS, execution_estimates
from data_collection.exchanges.registry import REGISTRY
from web.execution_fees import FEE_SCHEDULES
from web.execution_reader import ExecutionReader, ReaderBusy, ReaderFailure

bp = Blueprint("execution_api", __name__, url_prefix="/api/execution")


def execution_assets(registry) -> list[dict[str, str]]:
    return [
        {"id": asset, "label": registry.metadata(asset).label}
        for asset in registry.assets()
        if registry.metadata(asset).family == "crypto"
        and FEE_SCHEDULES.keys() & registry.listings(asset).keys()
    ]


@bp.after_request
def no_store(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.errorhandler(413)
def oversized_request(_error):
    return jsonify(error="Execution request is too large."), 413


def _request_parameters(registry):
    request.max_content_length = 8192
    try:
        body = request.get_json(silent=True)
    except RecursionError:
        body = None
    if (request.args or not isinstance(body, dict)
            or body.keys() - {"asset", "side", "fees"}
            or not {"asset", "side"} <= body.keys()):
        raise ValueError("Supply a coin, buy/sell side and optional venue fee tiers.")
    asset = body["asset"]
    if not isinstance(asset, str) or asset not in {choice["id"] for choice in execution_assets(registry)}:
        raise ValueError("Choose a configured crypto coin with perpetual listings.")
    if body["side"] not in ("buy", "sell"):
        raise ValueError("Side must be buy or sell.")
    requested_fees = body.get("fees", {})
    if not isinstance(requested_fees, dict) or requested_fees.keys() - FEE_SCHEDULES.keys():
        raise ValueError("Fee selections must use supported venue IDs.")
    selected = {}
    for venue, schedule in FEE_SCHEDULES.items():
        tier_id = requested_fees.get(venue, schedule["default_tier"])
        tier = next((tier for tier in schedule["tiers"] if tier["id"] == tier_id), None)
        if tier is None:
            raise ValueError(f"Unknown fee tier for {schedule['label']}.")
        selected[venue] = tier
    available = registry.listings(asset)
    listings = {venue: available[venue] for venue in FEE_SCHEDULES if venue in available}
    return asset, Side(body["side"]), listings, selected


def _unavailable_cells(quantities):
    return [
        {"notional_usd": str(notional), "quantity": quantity, "status": "unavailable",
         "complete": False, "filled_quantity": "0", "fill_percent": "0",
         "average_price": None, "average_price_usd": None, "effective_price_usd": None,
         "top_of_book_price": None, "top_of_book_price_usd": None,
         "fee_per_coin": None, "fee_per_coin_usd": None,
         "fee_usd": None, "price_impact_bps": None, "all_in_cost_bps": None, "best": False}
        for notional, quantity in zip(NOTIONALS, quantities)
    ]


@bp.post("")
def quote():
    registry = current_app.extensions["symbols"]
    try:
        asset, side, listings, selected = _request_parameters(registry)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    requested_at = datetime.now(timezone.utc)
    reader: ExecutionReader = current_app.extensions["execution_reader"]
    try:
        snapshot = reader.capture(listings)
    except ReaderBusy:
        return jsonify(error="Execution reader is busy. Refresh again shortly."), 503
    except ReaderFailure:
        return jsonify(error="Public execution snapshots are unavailable. Refresh again shortly."), 502
    now = datetime.now(timezone.utc)
    fees = {
        venue: Decimal(tier["taker_bps"]) if tier["taker_bps"] is not None else None
        for venue, tier in selected.items()
    }
    result = execution_estimates(
        snapshot["books"], side, fees,
        {currency: Decimal(rate) for currency, rate in snapshot["fx"]["rates"].items()}, now,
    )
    books = {book.venue: book for book in snapshot["books"]}
    rows = []
    for venue, symbol in listings.items():
        schedule = FEE_SCHEDULES[venue]
        tier = selected[venue]
        book = books.get(venue)
        row = result["venues"].get(venue)
        if row is None:
            row = {
                "status": "unavailable", "captured_at": None, "fetched_at": None,
                "timestamp_basis": None, "estimates": _unavailable_cells(result["quantities"]),
            }
        identity = registry.venue_identity(REGISTRY[venue].exchange, symbol)
        row.update(
            venue=venue, identity=identity.key, label=schedule["label"], symbol=symbol,
            quote_currency=book.quote_currency if book else None,
            error=snapshot["errors"].get(venue), notes=list(book.notes) if book else [],
            limits={
                "max_quantity": str(book.max_quantity) if book and book.max_quantity is not None else None,
                "max_quote_notional": str(book.max_quote_notional) if book and book.max_quote_notional is not None else None,
            },
            depth={"bid_levels": len(book.bids) if book else 0, "ask_levels": len(book.asks) if book else 0},
            fee={"tier": tier["id"], "tier_label": tier["label"], "taker_bps": tier["taker_bps"],
                 "source_url": schedule["source_url"], "checked_at": schedule["checked_at"],
                 "notes": schedule["notes"]},
        )
        rows.append(row)
    return jsonify(
        asset=asset, asset_label=registry.metadata(asset).label, side=side.value,
        requested_at=requested_at.isoformat(), completed_at=now.isoformat(),
        reference_price_usd=result["reference_price_usd"], notionals=result["notionals"],
        quantities=result["quantities"], fx=snapshot["fx"], venues=rows,
    )
