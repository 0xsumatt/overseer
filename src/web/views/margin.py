"""Margin calculator: the capital each venue needs to keep a position, or a
two-position pair on one venue, open through a chosen adverse price move; what
it costs to open, hold and close; and which venue is cheapest, from public
books, margin rules and stored funding."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext

from flask import Blueprint, current_app, jsonify, request

from core import execution_fees
from core.enums import Side
from core.execution import round_trip_estimates, usd_rate
from core.execution_funding import HOLD_HOURS
from core.margin import Position, capital_cost, same_safety
from data_collection.exchanges.registry import REGISTRY
from data_collection.execution_reader import ExecutionReader, ReaderBusy, ReaderFailure
from web.views.execution import execution_assets, holding_funding_for

bp = Blueprint("margin_api", __name__, url_prefix="/api/margin")

MIN_SIZE, MAX_SIZE = Decimal(100), Decimal(50_000_000)
MIN_MOVE, MAX_MOVE = Decimal("0.1"), Decimal(100)   # percent of entry
DEFAULT_MOVE = "10"
RANKS = ("cost", "capital")


@bp.after_request
def no_store(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.errorhandler(413)
def oversized_request(_error):
    return jsonify(error="Margin request is too large."), 413


def _decimal(value, low: Decimal, high: Decimal, message: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(message)
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError(message) from None
    if not result.is_finite() or not low <= result <= high:
        raise ValueError(message)
    return result


def _leg(body, offered: set[str], label: str) -> tuple[str, bool, Decimal]:
    if not isinstance(body, dict) or body.keys() != {"asset", "direction", "size"}:
        raise ValueError(f"The {label} needs a coin, a direction and a size.")
    if body["asset"] not in offered:
        raise ValueError("Choose a configured crypto coin with perpetual listings.")
    if body["direction"] not in ("long", "short"):
        raise ValueError("Direction must be long or short.")
    size = _decimal(body["size"], MIN_SIZE, MAX_SIZE,
                    f"Size must be between ${MIN_SIZE:,.0f} and ${MAX_SIZE:,.0f}.")
    return body["asset"], body["direction"] == "long", size


def _parameters(registry):
    request.max_content_length = 8192
    try:
        body = request.get_json(silent=True)
    except RecursionError:
        body = None
    allowed = {"position", "pair", "move", "hold_hours", "fees", "rate", "rank"}
    if request.args or not isinstance(body, dict) or body.keys() - allowed or "position" not in body:
        raise ValueError("Supply a position and optional pair, move, hold, fee tiers, capital rate and ranking.")
    offered = {choice["id"] for choice in execution_assets(registry)}
    legs = [_leg(body["position"], offered, "position")]
    if body.get("pair") is not None:
        legs.append(_leg(body["pair"], offered, "second position"))
        if legs[1][0] == legs[0][0]:
            raise ValueError("A pair needs two different coins.")
    move = _decimal(body.get("move", DEFAULT_MOVE), MIN_MOVE, MAX_MOVE,
                    f"The move to survive must be between {MIN_MOVE}% and {MAX_MOVE}%.")
    hold = body.get("hold_hours", 0)
    if type(hold) is not int or hold not in HOLD_HOURS:
        raise ValueError(f"Hold must be one of {', '.join(map(str, HOLD_HOURS))} hours.")
    rate = _decimal(body.get("rate", "0"), Decimal(0), Decimal(100), "Capital rate must be between 0% and 100% a year.")
    rank = body.get("rank", "cost")
    if rank not in RANKS:
        raise ValueError("Rank must be cost or capital.")
    fees = body.get("fees", {})
    if not isinstance(fees, dict):
        raise ValueError("Fee selections must use supported venue IDs.")
    return legs, move, hold, rate / 100, rank, execution_fees.selected_tiers(fees)


def _text(value) -> str | None:
    return str(value) if value is not None else None


def _entry(cell, long: bool) -> tuple[Decimal, Decimal] | None:
    """The common base quantity and this venue's average entry price, or None
    without a complete fill."""
    entry_leg = cell.get("buy" if long else "sell") if cell else None
    if entry_leg is None or entry_leg.get("average_price") is None or cell.get("quantity") is None:
        return None
    return Decimal(cell["quantity"]), Decimal(entry_leg["average_price"])


def _combined(safety, usd: Decimal) -> dict:
    """The venue's capital and maintenance for all legs together, in USD."""
    return {
        "capital_usd": str(safety.capital * usd), "minimum_usd": str(safety.minimum * usd),
        "capped": safety.capped, "leverage": str(safety.leverage), "loss_usd": str(safety.loss * usd),
        "moved_maintenance_usd": str(safety.moved_maintenance * usd),
        "maintenance_usd": str(safety.maintenance * usd), "alone_usd": str(safety.alone * usd),
        "offset_usd": str((safety.alone - safety.capital) * usd),
        "liquidation_price": _text(safety.liquidation_price), "liquidation_move": _text(safety.liquidation_move),
    }


@bp.post("")
def calculate():
    registry = current_app.extensions["symbols"]
    try:
        legs, move_percent, hold, annual_rate, rank, selected = _parameters(registry)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    move = move_percent / 100
    requested_at = datetime.now(timezone.utc)
    listings = [execution_fees.execution_listings(registry, asset) for asset, _long, _size in legs]
    venues = [venue for venue in listings[0] if all(venue in each for each in listings)]
    reader: ExecutionReader = current_app.extensions["execution_reader"]
    snapshots = []
    try:
        # One capture per coin, one after another, so a request holds one reader slot.
        for leg_listings in listings:
            snapshots.append(reader.capture({venue: leg_listings[venue] for venue in venues}, margin=True))
    except ReaderBusy:
        return jsonify(error="Execution reader is busy. Try again shortly."), 503
    except ReaderFailure:
        return jsonify(error="Public execution snapshots are unavailable. Try again shortly."), 502
    now = datetime.now(timezone.utc)
    fees = {venue: Decimal(tier["taker_bps"]) if tier["taker_bps"] is not None else None
            for venue, tier in selected.items()}
    notional = sum(size for _asset, _long, size in legs)
    priced = []
    for (_asset, long, size), leg_listings, snapshot in zip(legs, listings, snapshots):
        funding = holding_funding_for({venue: leg_listings[venue] for venue in venues}, hold, long, now)
        funding_bps = None if funding is None else {
            venue: Decimal(info["cost_bps"]) if info["cost_bps"] is not None else None for venue, info in funding.items()}
        fx = {currency: Decimal(value) for currency, value in snapshot["fx"]["rates"].items()}
        estimates = round_trip_estimates(snapshot["books"], Side.BUY if long else Side.SELL, fees, fx, now,
                                         funding_bps, notionals=(size,))
        priced.append((estimates, funding or {}, fx, {book.venue: book for book in snapshot["books"]}))

    rows = []
    with localcontext() as context:
        context.prec = 40
        for venue in venues:
            fee_schedule = execution_fees.FEE_SCHEDULES[venue]
            leg_rows, positions, rates, reasons = [], [], [], []
            for (asset, long, _size), snapshot, (estimates, funding, fx, books) in zip(legs, snapshots, priced):
                cell = (estimates["venues"].get(venue) or {}).get("estimates", [None])[0]
                schedule = snapshot["margins"].get(venue)
                book = books.get(venue)
                rate = usd_rate(book.quote_currency, fx) if book else None
                entry = _entry(cell, long)
                leg_rows.append({
                    "asset": asset, "direction": "long" if long else "short",
                    "symbol": book.symbol if book else None, "quote_currency": book.quote_currency if book else None,
                    "estimate": cell, "funding": funding.get(venue), "max_leverage": None, "maintenance_usd": None,
                    "maintenance_rate": None, "moved_maintenance_usd": None, "loss_usd": None, "alone_usd": None,
                })
                rates.append(rate)
                if cell is None or cell["status"] != "ok":
                    reasons.append(cell["status"] if cell else "unavailable")
                elif schedule is None:
                    reasons.append("margin_unavailable")
                if schedule is not None and entry is not None and rate is not None:
                    positions.append(Position(schedule, long, *entry))
            combined = costs = None
            # A venue's legs share its quote currency, so one rate converts the portfolio.
            if len(positions) == len(legs) and len({row["quote_currency"] for row in leg_rows}) == 1:
                safety = same_safety(positions, move)
                if safety is None:
                    reasons.append("above_margin_limit")
                else:
                    usd = rates[0]
                    for row, leg in zip(leg_rows, safety.legs):
                        row.update(max_leverage=str(leg.max_leverage), maintenance_usd=str(leg.maintenance * usd),
                                   maintenance_rate=str(leg.maintenance_rate),
                                   moved_maintenance_usd=str(leg.moved_maintenance * usd),
                                   loss_usd=str(leg.loss * usd), alone_usd=str(leg.alone * usd))
                    combined = _combined(safety, usd)
            if not reasons and combined is not None:
                trading = sum(Decimal(row["estimate"]["trading_cost_usd"]) for row in leg_rows)
                funding_usd = sum(Decimal(row["estimate"]["funding_usd"] or 0) for row in leg_rows)
                capital = capital_cost(Decimal(combined["capital_usd"]), annual_rate, hold)
                total = trading + funding_usd + capital
                costs = {"trading_usd": str(trading), "funding_usd": str(funding_usd), "capital_usd": str(capital),
                         "total_usd": str(total), "total_bps": str(total / notional * 10000)}
            first_schedule = snapshots[0]["margins"].get(venue)
            errors = [snapshot["errors"].get(venue) or snapshot["margin_errors"].get(venue) for snapshot in snapshots]
            rows.append({
                "venue": venue, "label": fee_schedule["label"],
                "identity": registry.venue_identity(REGISTRY[venue].exchange, listings[0][venue]).key,
                "status": "ok" if costs is not None else (reasons[0] if reasons else "unavailable"),
                "error": next((error for error in errors if error), None),
                "fee": {"tier_label": selected[venue]["label"], "taker_bps": selected[venue]["taker_bps"]},
                "margin": {
                    "source": first_schedule.source, "source_url": first_schedule.source_url,
                    "notes": list(first_schedule.notes), "portfolio": first_schedule.bulk is not None,
                } if first_schedule else None,
                "legs": leg_rows, "combined": combined, "costs": costs, "best": False,
            })
        ranked = [row for row in rows if row["status"] == "ok"]
        if ranked:
            def key(row):
                return Decimal(row["costs"]["total_usd"] if rank == "cost" else row["combined"]["capital_usd"])
            best = min(key(row) for row in ranked)
            for row in ranked:
                row["best"] = key(row) == best
    return jsonify(
        legs=[{"asset": asset, "direction": "long" if long else "short", "size_usd": str(size)}
              for asset, long, size in legs],
        move=format(move_percent, "f"), hold_hours=hold, rate=str(annual_rate * 100), rank=rank,
        requested_at=requested_at.isoformat(), completed_at=now.isoformat(),
        reference_prices=[estimates["reference_price_usd"] for estimates, _f, _x, _b in priced],
        venues=rows,
    )
