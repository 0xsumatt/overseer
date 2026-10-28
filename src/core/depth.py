"""Order-book depth near the mid, stored for historic heatmaps and backtests.

Each stored book keeps resting base quantity in fixed buckets out to
WINDOW_BPS either side of its own mid, plus how far the book itself reached.
Buckets are relative to the mid, so every row has one shape whatever the price
or tick size; the mid turns a bucket back into a price for drawing. Reach
separates "nothing resting here" from "beyond what this book showed": a REST
snapshot of Binance BTC reaches about 16 bps, Hyperliquid's 20 levels about
2 bps. Rows record their source and bucket width, so a later feed with a finer
cadence or resolution, such as a full WebSocket book, can share the table.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from core.execution import BookLevel, book_status, usd_rate

BUCKET_BPS = 1
WINDOW_BPS = 100
SOURCE_REST = "rest"
# Snapshots land every five minutes; a book older than this no longer
# describes its own slot.
MAX_BOOK_AGE_SECONDS = 60

# Heatmap windows: (hours shown, seconds per column, label). Each column is
# one stored snapshot, the latest in its slot; long windows skip snapshots
# rather than average them, since buckets follow a moving mid.
HEATMAP_WINDOWS = ((6, 300, "6h"), (24, 300, "24h"), (168, 1800, "7d"), (720, 7200, "30d"))


@dataclass(frozen=True, slots=True)
class DepthSnapshot:
    ts: datetime                # capture time, shared by every book in one capture
    asset: str
    venue: str
    source: str
    bucket_bps: float
    mid: Decimal                # native quote per coin
    quote_currency: str
    usd_rate: Decimal | None    # USD per unit of the quote currency, when known
    book_age_seconds: Decimal   # capture time minus the book's own timestamp
    bid_reach_bps: float        # how far below the mid the visible bids went
    ask_reach_bps: float
    bid_levels: int
    ask_levels: int
    bids: tuple[float, ...]     # base quantity per bucket below the mid, nearest first
    asks: tuple[float, ...]     # base quantity per bucket above the mid, nearest first


def _bucketed(levels: Sequence[BookLevel], mid: Decimal, below: bool) -> tuple[tuple[float, ...], float]:
    sizes = [0.0] * (WINDOW_BPS // BUCKET_BPS)
    reach = 0.0
    for level in levels:
        distance = float(((mid - level.price) if below else (level.price - mid)) / mid * 10000)
        reach = max(reach, distance)
        index = math.floor(distance / BUCKET_BPS)
        if 0 <= index < len(sizes):
            sizes[index] += float(level.quantity)
    return tuple(sizes), reach


def depth_snapshots(asset: str, snapshot: Mapping[str, Any], now: datetime) -> list[DepthSnapshot]:
    """One row per usable book in a capture. A book with an empty side, or with
    unsorted, non-positive or crossed levels, has no meaningful mid; a book
    older than MAX_BOOK_AGE_SECONDS no longer describes its slot. Both are
    skipped."""
    rates = {currency: Decimal(rate) for currency, rate in snapshot["fx"]["rates"].items()}
    rows = []
    for book in snapshot["books"]:
        age = (now - book.timestamp).total_seconds()
        if (not (book.bids and book.asks) or book_status(book, now) not in ("ok", "stale")
                or age > MAX_BOOK_AGE_SECONDS):
            continue
        mid = (book.bids[0].price + book.asks[0].price) / 2
        bids, bid_reach = _bucketed(book.bids, mid, below=True)
        asks, ask_reach = _bucketed(book.asks, mid, below=False)
        rows.append(DepthSnapshot(
            now, asset, book.venue, SOURCE_REST, BUCKET_BPS, mid, book.quote_currency,
            usd_rate(book.quote_currency, rates), Decimal(str(round(age, 3))),
            round(bid_reach, 3), round(ask_reach, 3), len(book.bids), len(book.asks), bids, asks,
        ))
    return rows
