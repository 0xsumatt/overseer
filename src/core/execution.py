"""Snapshot-only market-order estimates; never extrapolate uncaptured depth."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Mapping, Sequence

from core.enums import Side

NOTIONALS = tuple(Decimal(value) for value in ("10000", "100000", "1000000", "10000000"))
MAX_BOOK_AGE_SECONDS = 10


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class DepthBook:
    venue: str
    symbol: str
    quote_currency: str
    captured_at: datetime
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    venue_at: datetime | None = None
    max_quantity: Decimal | None = None
    max_quote_notional: Decimal | None = None
    notes: tuple[str, ...] = ()

    @property
    def timestamp(self) -> datetime:
        return self.venue_at or self.captured_at


def _valid_levels(levels: Sequence[BookLevel], *, bids: bool) -> bool:
    previous = None
    for level in levels:
        if not (level.price.is_finite() and level.quantity.is_finite()
                and level.price > 0 and level.quantity > 0):
            return False
        if previous is not None and (level.price > previous if bids else level.price < previous):
            return False
        previous = level.price
    return True


def book_status(book: DepthBook, now: datetime) -> str:
    """ok, stale, invalid_book (unsorted, non-positive or crossed levels) or
    invalid_timestamp."""
    if not (_valid_levels(book.bids, bids=True) and _valid_levels(book.asks, bids=False)):
        return "invalid_book"
    if book.bids and book.asks and book.bids[0].price > book.asks[0].price:
        return "invalid_book"
    if book.timestamp.tzinfo is None:
        return "invalid_timestamp"
    age = (now - book.timestamp).total_seconds()
    if age < -5:
        return "invalid_timestamp"
    if age > MAX_BOOK_AGE_SECONDS:
        return "stale"
    return "ok"


def usd_rate(currency: str, rates: Mapping[str, Decimal]) -> Decimal | None:
    """USD per unit of a quote currency; None unless known, finite and positive."""
    rate = Decimal(1) if currency == "USD" else rates.get(currency)
    return rate if rate is not None and rate.is_finite() and rate > 0 else None


def _walk(levels: Sequence[BookLevel], quantity: Decimal) -> tuple[Decimal, Decimal, bool]:
    remaining = quantity
    quote = Decimal(0)
    for level in levels:
        take = min(remaining, level.quantity)
        quote += take * level.price
        remaining -= take
        if remaining == 0:
            break
    return quantity - remaining, quote, remaining == 0


def execution_estimates(
    books: Sequence[DepthBook],
    side: Side,
    fees_bps: Mapping[str, Decimal | None],
    fx_rates: Mapping[str, Decimal],
    now: datetime,
    funding_bps: Mapping[str, Decimal | None] | None = None,
    notionals: Sequence[Decimal] = NOTIONALS,
) -> dict:
    """Compare identical base quantities, not different fills for the same spend.

    USD notionals (the presets unless given) become base size at the median
    fresh USD midpoint.
    Fees adjust the average price upward for buys and downward for sells.
    Unknown fees/FX and stale or incomplete books cannot win a comparison.
    With funding_bps (a holding period; positive means the position pays),
    each price also carries that funding and a venue without it cannot win.
    """
    if side not in (Side.BUY, Side.SELL):
        raise ValueError("Side must be buy or sell")
    statuses = {book.venue: book_status(book, now) for book in books}
    with localcontext() as context:
        context.prec = 40
        mids = []
        for book in books:
            rate = usd_rate(book.quote_currency, fx_rates)
            if statuses[book.venue] == "ok" and rate is not None and book.bids and book.asks:
                mids.append((book.bids[0].price + book.asks[0].price) / 2 * rate)
        mids.sort()
        middle = len(mids) // 2
        reference = (mids[middle] if len(mids) % 2 else (mids[middle - 1] + mids[middle]) / 2) if mids else None
        quantities = [notional / reference if reference is not None else None for notional in notionals]
        venues = {}
        for book in books:
            status = statuses[book.venue]
            rate = usd_rate(book.quote_currency, fx_rates)
            fee = fees_bps.get(book.venue)
            if fee is not None and (not fee.is_finite() or not -10000 < fee < 10000):
                raise ValueError("Invalid taker fee")
            if status == "ok" and rate is None:
                status = "fx_missing"
            if status == "ok" and fee is None:
                status = "fee_unknown"
            funding = funding_bps.get(book.venue) if funding_bps is not None else None
            if status == "ok" and funding_bps is not None and funding is None:
                status = "funding_unknown"
            levels = book.asks if side == Side.BUY else book.bids
            cells = []
            for notional, quantity in zip(notionals, quantities):
                cell = {
                    "notional_usd": str(notional),
                    "quantity": str(quantity) if quantity is not None else None,
                    "status": status if status != "ok" else "no_reference",
                    "complete": False,
                    "filled_quantity": "0",
                    "fill_percent": "0",
                    "average_price": None,
                    "average_price_usd": None,
                    "top_of_book_price": None,
                    "top_of_book_price_usd": None,
                    "fee_per_coin": None,
                    "fee_per_coin_usd": None,
                    "effective_price_usd": None,
                    "all_in_cost_bps": None,
                    "fee_usd": None,
                    "price_impact_bps": None,
                    "funding_bps": None,
                    "funding_usd": None,
                    "held_price_usd": None,
                    "total_cost_bps": None,
                    "best": False,
                }
                if quantity is not None and statuses[book.venue] == "ok":
                    if book.max_quantity is not None and quantity > book.max_quantity:
                        cell["status"] = "order_limit"
                        cells.append(cell)
                        continue
                    filled, quote, complete = _walk(levels, quantity)
                    cell.update(complete=complete, filled_quantity=str(filled),
                                fill_percent=str(filled / quantity * 100))
                    if not complete:
                        cell["status"] = "insufficient_depth"
                    elif book.max_quote_notional is not None and quote > book.max_quote_notional:
                        cell["status"] = "order_limit"
                    else:
                        average = quote / quantity
                        top_price = levels[0].price
                        cell["top_of_book_price"] = str(top_price)
                        cell["average_price"] = str(average)
                        impact = (
                            (average / top_price - 1 if side == Side.BUY
                             else 1 - average / top_price) * 10000
                        )
                        cell["price_impact_bps"] = str(impact)
                        if fee is not None:
                            fee_rate = fee / 10000
                            cell["fee_per_coin"] = str(average * fee_rate)
                            cell["all_in_cost_bps"] = str(fee + impact)
                            if funding is not None:
                                cell["funding_bps"] = str(funding)
                                cell["total_cost_bps"] = str(fee + impact + funding)
                        if rate is not None:
                            cell["top_of_book_price_usd"] = str(top_price * rate)
                            average_usd = average * rate
                            cell["average_price_usd"] = str(average_usd)
                            if fee is not None:
                                cell["fee_per_coin_usd"] = str(average_usd * fee_rate)
                                cell["fee_usd"] = str(quote * rate * fee_rate)
                                effective = average_usd * (1 + fee_rate if side == Side.BUY else 1 - fee_rate)
                                cell["effective_price_usd"] = str(effective)
                                if funding is not None:
                                    # Funding on the trade's value; a cost raises a buy and lowers a sell.
                                    funding_rate = funding / 10000
                                    cell["funding_usd"] = str(quote * rate * funding_rate)
                                    cell["held_price_usd"] = str(
                                        effective * (1 + funding_rate if side == Side.BUY else 1 - funding_rate))
                        cell["status"] = status
                cells.append(cell)
            venues[book.venue] = {
                "status": status,
                "captured_at": book.timestamp.isoformat(),
                "fetched_at": book.captured_at.isoformat(),
                "timestamp_basis": "venue" if book.venue_at is not None else "fetch",
                "estimates": cells,
            }
        ranked = "held_price_usd" if funding_bps is not None else "effective_price_usd"
        for index in range(len(notionals)):
            candidates = [row["estimates"][index] for row in venues.values()
                          if row["estimates"][index]["status"] == "ok"]
            if candidates:
                best = (min if side == Side.BUY else max)(Decimal(cell[ranked]) for cell in candidates)
                for cell in candidates:
                    cell["best"] = Decimal(cell[ranked]) == best
        return {
            "reference_price_usd": str(reference) if reference is not None else None,
            "notionals": [str(value) for value in notionals],
            "quantities": [str(value) if value is not None else None for value in quantities],
            "venues": venues,
        }


# Most to least fundamental, so a round trip reports the reason that matters.
_SEVERITY = ("invalid_book", "invalid_timestamp", "stale", "no_reference", "order_limit",
             "insufficient_depth", "fx_missing", "fee_unknown", "funding_unknown")


def _combined_status(buy: str, sell: str) -> str:
    failing = [status for status in (buy, sell) if status != "ok"]
    if not failing:
        return "ok"
    return min(failing, key=lambda status: _SEVERITY.index(status) if status in _SEVERITY else -1)


def _round_trip_cell(buy: dict, sell: dict, first: Side, holding: bool,
                     funding: Decimal | None, reference: Decimal | None) -> dict:
    status = _combined_status(buy["status"], sell["status"])
    if status == "ok" and holding and funding is None:
        status = "funding_unknown"
    short_side = None
    if status == "insufficient_depth":
        short_side = "buy" if Decimal(buy["fill_percent"]) <= Decimal(sell["fill_percent"]) else "sell"
    cell = {
        "notional_usd": buy["notional_usd"],
        "quantity": buy["quantity"],
        "status": status,
        "complete": buy["complete"] and sell["complete"],
        "fill_percent": min(buy["fill_percent"], sell["fill_percent"], key=Decimal),
        "short_side": short_side,
        "buy": {key: value for key, value in buy.items() if key != "best"},
        "sell": {key: value for key, value in sell.items() if key != "best"},
        "spread_slippage": None,
        "spread_slippage_usd": None,
        "spread_slippage_bps": None,
        "fees_usd": None,
        "trading_cost_usd": None,
        "trading_cost_bps": None,
        "funding_bps": str(funding) if holding and funding is not None else None,
        "funding_usd": None,
        "total_cost_usd": None,
        "total_cost_bps": None,
        "best": False,
    }
    if buy["average_price"] is None or sell["average_price"] is None:
        return cell
    quantity = Decimal(buy["quantity"])
    cell["spread_slippage"] = str(quantity * (Decimal(buy["average_price"]) - Decimal(sell["average_price"])))
    if buy["average_price_usd"] is None or sell["average_price_usd"] is None:
        return cell
    trade_value = quantity * reference
    gross = quantity * (Decimal(buy["average_price_usd"]) - Decimal(sell["average_price_usd"]))
    cell["spread_slippage_usd"] = str(gross)
    cell["spread_slippage_bps"] = str(gross / trade_value * 10000)
    if buy["fee_usd"] is None or sell["fee_usd"] is None:
        return cell
    fees = Decimal(buy["fee_usd"]) + Decimal(sell["fee_usd"])
    trading = gross + fees
    cell["fees_usd"] = str(fees)
    cell["trading_cost_usd"] = str(trading)
    cell["trading_cost_bps"] = str(trading / trade_value * 10000)
    total = trading
    if holding:
        if funding is None:
            return cell
        entry = buy if first == Side.BUY else sell
        funding_usd = quantity * Decimal(entry["average_price_usd"]) * funding / 10000
        cell["funding_usd"] = str(funding_usd)
        total += funding_usd
    cell["total_cost_usd"] = str(total)
    cell["total_cost_bps"] = str(total / trade_value * 10000)
    return cell


def round_trip_estimates(
    books: Sequence[DepthBook],
    first: Side,
    fees_bps: Mapping[str, Decimal | None],
    fx_rates: Mapping[str, Decimal],
    now: datetime,
    funding_bps: Mapping[str, Decimal | None] | None = None,
    notionals: Sequence[Decimal] = NOTIONALS,
) -> dict:
    """Open and close the same base quantity against one capture of the books.

    The cost is the fee-adjusted buy minus the fee-adjusted sell, so spread,
    slippage and both taker fees count while a venue's price level cancels
    out. `first` is the opening side; with funding_bps (a holding period;
    positive means that position pays) funding on the opening leg is added.
    The lowest total cost wins.
    """
    buy = execution_estimates(books, Side.BUY, fees_bps, fx_rates, now, notionals=notionals)
    sell = execution_estimates(books, Side.SELL, fees_bps, fx_rates, now, notionals=notionals)
    holding = funding_bps is not None
    with localcontext() as context:
        context.prec = 40
        reference = Decimal(buy["reference_price_usd"]) if buy["reference_price_usd"] is not None else None
        venues = {}
        for venue, buy_row in buy["venues"].items():
            funding = funding_bps.get(venue) if holding else None
            status = buy_row["status"]
            if status == "ok" and holding and funding is None:
                status = "funding_unknown"
            cells = [
                _round_trip_cell(buy_cell, sell_cell, first, holding, funding, reference)
                for buy_cell, sell_cell in zip(buy_row["estimates"], sell["venues"][venue]["estimates"])
            ]
            venues[venue] = {**buy_row, "status": status, "estimates": cells}
        for index in range(len(notionals)):
            candidates = [row["estimates"][index] for row in venues.values()
                          if row["estimates"][index]["status"] == "ok"]
            if candidates:
                best = min(Decimal(cell["total_cost_usd"]) for cell in candidates)
                for cell in candidates:
                    cell["best"] = Decimal(cell["total_cost_usd"]) == best
    return {**buy, "venues": venues}
