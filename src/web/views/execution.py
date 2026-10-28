"""On-demand, public, read-only perpetual execution estimates."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from flask import Blueprint, current_app, jsonify, request

from core.enums import Side
from core.execution import NOTIONALS, execution_estimates, round_trip_estimates
# Through the module, not name imports: the listing and tier helpers read
# execution_fees.FEE_SCHEDULES at call time, so every lookup here must too.
from core import execution_fees
from core.execution_funding import (
    CURRENT_HOLD_LIMIT_HOURS, HOLD_HOURS, SETTLED_WINDOW, funding_unavailable, holding_funding,
)
from data_collection.exchanges.registry import REGISTRY
from data_collection.execution_reader import ExecutionReader, ReaderBusy, ReaderFailure

# Each trade's opening side. It prices one-way trades and sets the held
# position's direction: a buy opens a long, a sell a short.
TRADES = {"buy": Side.BUY, "sell": Side.SELL, "buy_sell": Side.BUY, "sell_buy": Side.SELL}
ROUND_TRIPS = ("buy_sell", "sell_buy")

bp = Blueprint("execution_api", __name__, url_prefix="/api/execution")


def execution_assets(registry) -> list[dict[str, str]]:
    return [
        {"id": asset, "label": registry.metadata(asset).label}
        for asset in registry.assets()
        if registry.metadata(asset).family == "crypto"
        and execution_fees.FEE_SCHEDULES.keys() & registry.listings(asset).keys()
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
            or body.keys() - {"asset", "side", "fees", "hold_hours"}
            or not {"asset", "side"} <= body.keys()):
        raise ValueError("Supply a coin, a trade, an optional hold and optional venue fee tiers.")
    asset = body["asset"]
    if not isinstance(asset, str) or asset not in {choice["id"] for choice in execution_assets(registry)}:
        raise ValueError("Choose a configured crypto coin with perpetual listings.")
    trade = body["side"]
    if not isinstance(trade, str) or trade not in TRADES:
        raise ValueError("Trade must be buy, sell, buy_sell or sell_buy.")
    hold = body.get("hold_hours", 0)
    if type(hold) is not int or hold not in HOLD_HOURS:
        raise ValueError(f"Hold must be one of {', '.join(map(str, HOLD_HOURS))} hours.")
    requested_fees = body.get("fees", {})
    if not isinstance(requested_fees, dict):
        raise ValueError("Fee selections must use supported venue IDs.")
    selected = execution_fees.selected_tiers(requested_fees)
    return asset, trade, hold, execution_fees.execution_listings(registry, asset), selected


def holding_funding_for(listings, hold: int, long: bool, now: datetime) -> dict[str, dict] | None:
    """Expected funding per listed venue over the hold; None without a hold."""
    if not hold:
        return None
    pairs = {venue: (REGISTRY[venue].exchange.value, symbol) for venue, symbol in listings.items()}
    exchanges = sorted({exchange for exchange, _ in pairs.values()})
    symbols = sorted({symbol for _, symbol in pairs.values()})
    store = current_app.extensions["read_storage"]
    current, settled = {}, defaultdict(list)
    try:
        if hold <= CURRENT_HOLD_LIMIT_HOURS:
            current = {(row["exchange"], row["symbol"]): row
                       for row in store.current_funding_rates(exchanges, symbols)}
        else:
            for row in store.settled_funding_rates(exchanges, symbols, now - SETTLED_WINDOW):
                settled[(row["exchange"], row["symbol"])].append(row)
    except Exception:
        # Funding is one input among several: the comparison still renders,
        # with every venue marked unrankable rather than silently unfunded.
        current_app.logger.exception("execution funding read failed")
        return funding_unavailable(listings, hold, "Funding data could not be read")
    return holding_funding(
        listings,
        {venue: current.get(pair) for venue, pair in pairs.items()},
        {venue: settled.get(pair, []) for venue, pair in pairs.items()},
        hold, long, now,
    )


_ONE_WAY_FIELDS = (
    "average_price", "average_price_usd", "effective_price_usd", "top_of_book_price",
    "top_of_book_price_usd", "fee_per_coin", "fee_per_coin_usd", "fee_usd", "price_impact_bps",
    "all_in_cost_bps", "funding_bps", "funding_usd", "held_price_usd", "total_cost_bps",
)
_ROUND_TRIP_FIELDS = (
    "short_side", "buy", "sell", "spread_slippage", "spread_slippage_usd", "spread_slippage_bps",
    "fees_usd", "trading_cost_usd", "trading_cost_bps", "funding_bps", "funding_usd",
    "total_cost_usd", "total_cost_bps",
)


def _unavailable_cells(quantities, round_trip: bool):
    fields = _ROUND_TRIP_FIELDS if round_trip else _ONE_WAY_FIELDS
    return [
        {"notional_usd": str(notional), "quantity": quantity, "status": "unavailable",
         "complete": False, "filled_quantity": "0", "fill_percent": "0",
         **dict.fromkeys(fields), "best": False}
        for notional, quantity in zip(NOTIONALS, quantities)
    ]


@bp.post("")
def quote():
    registry = current_app.extensions["symbols"]
    try:
        asset, trade, hold, listings, selected = _request_parameters(registry)
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
    first = TRADES[trade]
    funding = holding_funding_for(listings, hold, first == Side.BUY, now)
    funding_bps = None if funding is None else {
        venue: Decimal(info["cost_bps"]) if info["cost_bps"] is not None else None
        for venue, info in funding.items()
    }
    fx = {currency: Decimal(rate) for currency, rate in snapshot["fx"]["rates"].items()}
    estimate = round_trip_estimates if trade in ROUND_TRIPS else execution_estimates
    result = estimate(snapshot["books"], first, fees, fx, now, funding_bps)
    books = {book.venue: book for book in snapshot["books"]}
    rows = []
    for venue, symbol in listings.items():
        schedule = execution_fees.FEE_SCHEDULES[venue]
        tier = selected[venue]
        book = books.get(venue)
        row = result["venues"].get(venue)
        if row is None:
            row = {
                "status": "unavailable", "captured_at": None, "fetched_at": None,
                "timestamp_basis": None,
                "estimates": _unavailable_cells(result["quantities"], trade in ROUND_TRIPS),
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
            funding=funding.get(venue) if funding is not None else None,
        )
        rows.append(row)
    return jsonify(
        asset=asset, asset_label=registry.metadata(asset).label, side=trade, hold_hours=hold,
        requested_at=requested_at.isoformat(), completed_at=now.isoformat(),
        reference_price_usd=result["reference_price_usd"], notionals=result["notionals"],
        quantities=result["quantities"], fx=snapshot["fx"], venues=rows,
    )
