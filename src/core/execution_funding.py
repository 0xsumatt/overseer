"""Expected funding while a position is held, for the execution estimator.

Holds up to a day use each venue's current (next-settlement) rate, pro rata.
Longer holds repeat the previous 24 hours of settled funding instead: one live
rate is a noisy guide to a week, and a day of settlements is a steadier one.
Neither predicts future rates; both are labelled estimates.

Rates are fractions per interval; positive means longs pay shorts. Settled
rows are averaged per hour rather than summed, so a missed ingest does not
shrink the day and BULK, whose collector records its predicted hourly rate
(four times an hour until collection became hourly), is not counted more than
once. A venue that keeps a spread
of each payment (Bullet keeps 5% from each side) charges the payer
(1 + spread) times the payment and pays the receiver (1 - spread) times it,
settlement by settlement.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any, Iterable, Mapping, Sequence

HOLDS = ((0, "None"), (8, "8h"), (24, "24h"), (168, "7d"), (720, "30d"))
HOLD_HOURS = tuple(hours for hours, _label in HOLDS)
CURRENT_HOLD_LIMIT_HOURS = 24
# The funding page's rule for a live rate.
CURRENT_MAX_AGE = timedelta(minutes=15)
SETTLED_WINDOW = timedelta(hours=24)
# A day's average needs at least half of the day observed.
SETTLED_MIN_SPAN = timedelta(hours=12)


def _paid(hourly: Decimal, long: bool, spread: Decimal) -> Decimal:
    """What the position pays per hour at this rate, as a fraction of its value
    and negative when paid to it, after the venue's spread."""
    paid = hourly if long else -hourly
    return paid * (1 + spread) if paid > 0 else paid * (1 - spread)


def _current(row: Mapping[str, Any] | None, now: datetime, long: bool,
             spread: Decimal) -> tuple[dict, Decimal | None]:
    if not row:
        return {"basis": "current", "unavailable": "No current funding rate"}, None
    rate, interval, ts = row["rate"], Decimal(row["interval_hours"]), row["ts"]
    info = {"basis": "current", "rate": str(rate), "interval_hours": str(interval), "as_of": ts.isoformat()}
    age = now - ts
    if age > CURRENT_MAX_AGE:
        minutes = int(age.total_seconds() // 60)
        return {**info, "unavailable": f"Current funding rate is {minutes} minutes old"}, None
    return info, _paid(rate / interval, long, spread)


def _previous_day(rows: Sequence[Mapping[str, Any]], now: datetime, long: bool,
                  spread: Decimal) -> tuple[dict, Decimal | None]:
    window = sorted((row for row in rows if now - SETTLED_WINDOW < row["ts"] <= now and row["interval_hours"]),
                    key=lambda row: row["ts"])
    info = {"basis": "previous_24h", "settlements": len(window)}
    if not window:
        return {**info, "unavailable": "No settled funding in the previous 24 hours"}, None
    span = window[-1]["ts"] - window[0]["ts"] + timedelta(hours=float(window[-1]["interval_hours"]))
    rates = [Decimal(row["rate"]) / Decimal(row["interval_hours"]) for row in window]
    hourly = sum(rates) / len(rates)
    info.update(rate=str(hourly * 24), interval_hours="24", as_of=window[-1]["ts"].isoformat())
    if span < SETTLED_MIN_SPAN:
        hours = int(span.total_seconds() // 3600)
        return {**info, "unavailable": f"Settled funding covers only {hours} of the previous 24 hours"}, None
    return info, sum(_paid(rate, long, spread) for rate in rates) / len(rates)


def holding_funding(
    venues: Iterable[str],
    current: Mapping[str, Mapping[str, Any] | None],
    settled: Mapping[str, Sequence[Mapping[str, Any]]],
    hold_hours: int,
    long: bool,
    now: datetime,
    spreads: Mapping[str, Decimal] | None = None,
) -> dict[str, dict]:
    """Per venue: the rate used and `cost_bps`, the funding the position pays
    over the hold in bps of its value (negative when it is paid to the holder),
    after the share of each payment the venue keeps (`spreads`, a fraction per
    venue, echoed as `spread`). `cost_bps` is None, with an `unavailable`
    reason, when the rule's data is missing or too old; such a venue cannot be
    ranked.

    `current` rows carry rate, interval_hours and ts; `settled` rows the same,
    with ts the settlement time.
    """
    if hold_hours not in HOLD_HOURS or not hold_hours:
        raise ValueError("Hold must be one of the offered periods")
    out = {}
    with localcontext() as context:
        context.prec = 40
        for venue in venues:
            spread = (spreads or {}).get(venue, Decimal(0))
            if hold_hours <= CURRENT_HOLD_LIMIT_HOURS:
                info, hourly = _current(current.get(venue), now, long, spread)
            else:
                info, hourly = _previous_day(settled.get(venue, ()), now, long, spread)
            cost = None if hourly is None else hourly * hold_hours * 10000
            out[venue] = {"unavailable": None, **info, "hold_hours": hold_hours, "spread": str(spread),
                          "cost_bps": str(cost) if cost is not None else None}
    return out


def funding_unavailable(venues: Iterable[str], hold_hours: int, reason: str) -> dict[str, dict]:
    """Every venue unrankable for one reason, such as a failed read."""
    basis = "current" if hold_hours <= CURRENT_HOLD_LIMIT_HOURS else "previous_24h"
    return {venue: {"unavailable": reason, "basis": basis, "hold_hours": hold_hours, "cost_bps": None}
            for venue in venues}
