"""Scheduled cheapest-venue history for the execution estimator.

The scheduler captures public depth for a fixed coin list on a fixed clock and
stores each venue's pre-fee result per side and trade size. Sampling on a
clock, not on page visits, keeps "cheapest X% of the time" from meaning "X% of
the times someone looked". Fees are applied when statistics are read, so they
can follow base or user-selected tiers, and a fee-schedule change never
rewrites history.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from statistics import median
from typing import Any, Iterable, Mapping

from core.enums import Side
from core.execution import NOTIONALS, execution_estimates

HISTORY_ASSETS = ("BTC", "ETH", "SOL")
HISTORY_INTERVAL_SECONDS = 300
HISTORY_WINDOWS_DAYS = (7, 30)
# $10m is left out: Hyperliquid's public book shows at most 20 price levels a
# side, so at that size it reads "snapshot too shallow" and a ranking would
# reflect which books we can see rather than which venue is cheapest.
HISTORY_NOTIONALS = NOTIONALS[:3]


@dataclass(frozen=True, slots=True)
class ExecutionSample:
    ts: datetime
    asset: str
    side: str
    notional_usd: Decimal
    venue: str
    status: str                        # 'ok', or why the venue could not be compared
    average_price_usd: Decimal | None  # pre-fee average fill; set only when 'ok'
    book_age_seconds: Decimal | None   # capture time minus the book's own timestamp


def history_samples(
    asset: str,
    listings: Mapping[str, str],
    snapshot: Mapping[str, Any],
    now: datetime,
) -> list[ExecutionSample]:
    """One pre-fee sample per listed venue, side and history size.

    Eligibility (freshness, captured depth, venue order limits, FX) is exactly
    the live estimator's: estimates run with zero fees so that only the fees
    are left for read time. A listed venue that returned no book is recorded
    as unavailable rather than silently missing.
    """
    books = {book.venue: book for book in snapshot["books"] if book.venue in listings}
    fx = {currency: Decimal(rate) for currency, rate in snapshot["fx"]["rates"].items()}
    zero_fees = {venue: Decimal(0) for venue in listings}
    ages = {
        venue: Decimal(str(round((now - book.timestamp).total_seconds(), 3)))
        for venue, book in books.items()
    }
    samples = []
    for side in (Side.BUY, Side.SELL):
        result = execution_estimates(list(books.values()), side, zero_fees, fx, now)
        for venue in listings:
            row = result["venues"].get(venue)
            for index, notional in enumerate(NOTIONALS):
                if notional not in HISTORY_NOTIONALS:
                    continue
                cell = row["estimates"][index] if row else None
                status = cell["status"] if cell else "unavailable"
                price = Decimal(cell["average_price_usd"]) if status == "ok" else None
                samples.append(ExecutionSample(
                    now, asset, side.value, notional, venue, status, price, ages.get(venue),
                ))
    return samples


@dataclass
class _VenueTally:
    samples: int = 0
    quoted: int = 0
    ranked: int = 0
    wins: float = 0.0
    gaps: list[float] = field(default_factory=list)
    excluded: Counter = field(default_factory=Counter)


def _percent(part: float, whole: int) -> float | None:
    return round(part / whole * 100, 1) if whole else None


ROUND_TRIP = "round_trip"
HISTORY_SIDES = (Side.BUY.value, Side.SELL.value, ROUND_TRIP)


def _quotes(
    sample: list[Mapping[str, Any]], side: str, fees_bps: Mapping[str, float | None],
) -> Iterable[tuple[str, str, float | None, float | None]]:
    """(venue, status, cost, scale) for each venue in one capture. Cost is
    lower-is-better and None when the fee is unknown; scale is the price level
    that turns a cost gap into bps."""
    if side != ROUND_TRIP:
        sign = 1 if side == Side.BUY.value else -1
        for row in sample:
            fee = fees_bps.get(row["venue"])
            if row["status"] != "ok" or fee is None:
                yield row["venue"], row["status"], None, None
                continue
            price = float(row["average_price_usd"]) * (1 + sign * fee / 10000)
            yield row["venue"], "ok", sign * price, price
        return
    legs: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in sample:
        legs[row["venue"]][row["side"]] = row
    for venue, pair in legs.items():
        status = next((pair[leg]["status"] if leg in pair else "unavailable"
                       for leg in ("buy", "sell") if leg not in pair or pair[leg]["status"] != "ok"), "ok")
        fee = fees_bps.get(venue)
        if status != "ok" or fee is None:
            yield venue, status, None, None
            continue
        bought = float(pair["buy"]["average_price_usd"]) * (1 + fee / 10000)
        sold = float(pair["sell"]["average_price_usd"]) * (1 - fee / 10000)
        yield venue, "ok", bought - sold, (bought + sold) / 2


def summarise(
    rows: Iterable[Mapping[str, Any]],
    side: str,
    fees_bps: Mapping[str, float | None],
    notionals: Iterable[Decimal] = HISTORY_NOTIONALS,
) -> list[dict[str, Any]]:
    """Cheapest-venue statistics per trade size.

    rows carry ts, side, notional_usd, venue, status and average_price_usd
    (float, pre-fee). side is buy, sell or round_trip: a round trip pairs each
    venue's buy and sell from one capture, and its cost is the fee-adjusted
    buy minus the fee-adjusted sell. A comparison counts when at least two
    venues had complete, fresh quotes and a known fee; a tie splits the win. A
    venue's gap is how much worse than the best it was, in bps of the price,
    in the comparisons it did not win.
    """
    sizes = {int(notional): {"captures": 0, "ranked": 0, "venues": defaultdict(_VenueTally)}
             for notional in notionals}
    captures: dict[tuple[int, datetime], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if side != ROUND_TRIP and row.get("side", side) != side:
            continue
        notional = int(row["notional_usd"])
        if notional in sizes:
            captures[(notional, row["ts"])].append(row)
    for (notional, _ts), sample in captures.items():
        size = sizes[notional]
        size["captures"] += 1
        priced = []
        for venue, status, cost, scale in _quotes(sample, side, fees_bps):
            tally = size["venues"][venue]
            tally.samples += 1
            if status != "ok":
                tally.excluded[status] += 1
                continue
            tally.quoted += 1
            if cost is not None:
                priced.append((venue, cost, scale))
        if len(priced) < 2:
            continue
        size["ranked"] += 1
        best, best_scale = min((cost, scale) for _, cost, scale in priced)
        winners = sum(1 for _, cost, _ in priced if cost == best)
        for venue, cost, _ in priced:
            tally = size["venues"][venue]
            tally.ranked += 1
            if cost == best:
                tally.wins += 1 / winners
            else:
                tally.gaps.append((cost - best) / best_scale * 10000)
    summary = []
    for notional, size in sizes.items():
        venues = []
        for venue, tally in size["venues"].items():
            fee_known = fees_bps.get(venue) is not None
            venues.append({
                "venue": venue,
                "fee_known": fee_known,
                "win_share_pct": _percent(tally.wins, size["ranked"]) if fee_known else None,
                "median_gap_bps": round(median(tally.gaps), 2) if tally.gaps else None,
                "quoted_pct": _percent(tally.quoted, tally.samples),
                "samples": tally.samples,
                "excluded": dict(tally.excluded.most_common()),
            })
        # Ranked venues by win share; fee-unknown venues, which cannot win, last.
        venues.sort(key=lambda item: (
            item["win_share_pct"] is None, -(item["win_share_pct"] or 0),
            -(item["quoted_pct"] or 0), item["venue"],
        ))
        summary.append({
            "notional_usd": str(notional),
            "captures": size["captures"],
            "ranked": size["ranked"],
            "venues": venues,
        })
    return summary
