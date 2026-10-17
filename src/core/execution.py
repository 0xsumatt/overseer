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


def _book_status(book: DepthBook, now: datetime) -> str:
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


def _rate(currency: str, rates: Mapping[str, Decimal]) -> Decimal | None:
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
) -> dict:
    """Compare identical base quantities, not different fills for the same spend.

    Preset USD notionals become base size at the median fresh USD midpoint.
    Fees adjust the average price upward for buys and downward for sells.
    Unknown fees/FX and stale or incomplete books cannot win a comparison.
    """
    if side not in (Side.BUY, Side.SELL):
        raise ValueError("Side must be buy or sell")
    statuses = {book.venue: _book_status(book, now) for book in books}
    with localcontext() as context:
        context.prec = 40
        mids = []
        for book in books:
            rate = _rate(book.quote_currency, fx_rates)
            if statuses[book.venue] == "ok" and rate is not None and book.bids and book.asks:
                mids.append((book.bids[0].price + book.asks[0].price) / 2 * rate)
        mids.sort()
        middle = len(mids) // 2
        reference = (mids[middle] if len(mids) % 2 else (mids[middle - 1] + mids[middle]) / 2) if mids else None
        quantities = [notional / reference if reference is not None else None for notional in NOTIONALS]
        venues = {}
        for book in books:
            status = statuses[book.venue]
            rate = _rate(book.quote_currency, fx_rates)
            fee = fees_bps.get(book.venue)
            if fee is not None and (not fee.is_finite() or not -10000 < fee < 10000):
                raise ValueError("Invalid taker fee")
            if status == "ok" and rate is None:
                status = "fx_missing"
            if status == "ok" and fee is None:
                status = "fee_unknown"
            levels = book.asks if side == Side.BUY else book.bids
            cells = []
            for notional, quantity in zip(NOTIONALS, quantities):
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
                        if rate is not None:
                            cell["top_of_book_price_usd"] = str(top_price * rate)
                            average_usd = average * rate
                            cell["average_price_usd"] = str(average_usd)
                            if fee is not None:
                                cell["fee_per_coin_usd"] = str(average_usd * fee_rate)
                                cell["fee_usd"] = str(quote * rate * fee_rate)
                                effective = average_usd * (1 + fee_rate if side == Side.BUY else 1 - fee_rate)
                                cell["effective_price_usd"] = str(effective)
                        cell["status"] = status
                cells.append(cell)
            venues[book.venue] = {
                "status": status,
                "captured_at": book.timestamp.isoformat(),
                "fetched_at": book.captured_at.isoformat(),
                "timestamp_basis": "venue" if book.venue_at is not None else "fetch",
                "estimates": cells,
            }
        for index in range(len(NOTIONALS)):
            candidates = [row["estimates"][index] for row in venues.values()
                          if row["estimates"][index]["status"] == "ok"]
            if candidates:
                best = (min if side == Side.BUY else max)(Decimal(cell["effective_price_usd"]) for cell in candidates)
                for cell in candidates:
                    cell["best"] = Decimal(cell["effective_price_usd"]) == best
        return {
            "reference_price_usd": str(reference) if reference is not None else None,
            "notionals": [str(value) for value in NOTIONALS],
            "quantities": [str(value) if value is not None else None for value in quantities],
            "venues": venues,
        }
