"""Expected funding while a position is held, for the execution estimator.

Holds up to a day use each venue's current (next-settlement) rate, pro rata.
Longer holds repeat the previous 24 hours of settled funding instead: one live
rate is a noisy guide to a week, and a day of settlements is a steadier one.
Neither predicts future rates; both are labelled estimates.

Rates are fractions per interval; positive means longs pay shorts. Settled
rows are averaged per hour rather than summed, so a missed ingest does not
shrink the day and BULK, whose collector records its predicted hourly rate
every 15 minutes, is not counted four times over.
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


def _current(row: Mapping[str, Any] | None, now: datetime) -> tuple[dict, Decimal | None]:
    if not row:
        return {"basis": "current", "unavailable": "No current funding rate"}, None
    rate, interval, ts = row["rate"], Decimal(row["interval_hours"]), row["ts"]
    info = {"basis": "current", "rate": str(rate), "interval_hours": str(interval), "as_of": ts.isoformat()}
    age = now - ts
    if age > CURRENT_MAX_AGE:
        minutes = int(age.total_seconds() // 60)
        return {**info, "unavailable": f"Current funding rate is {minutes} minutes old"}, None
    return info, rate / interval


def _previous_day(rows: Sequence[Mapping[str, Any]], now: datetime) -> tuple[dict, Decimal | None]:
    window = sorted((row for row in rows if now - SETTLED_WINDOW < row["ts"] <= now and row["interval_hours"]),
                    key=lambda row: row["ts"])
    info = {"basis": "previous_24h", "settlements": len(window)}
    if not window:
        return {**info, "unavailable": "No settled funding in the previous 24 hours"}, None
    span = window[-1]["ts"] - window[0]["ts"] + timedelta(hours=float(window[-1]["interval_hours"]))
    hourly = sum(Decimal(row["rate"]) / Decimal(row["interval_hours"]) for row in window) / len(window)
    info.update(rate=str(hourly * 24), interval_hours="24", as_of=window[-1]["ts"].isoformat())
    if span < SETTLED_MIN_SPAN:
        hours = int(span.total_seconds() // 3600)
        return {**info, "unavailable": f"Settled funding covers only {hours} of the previous 24 hours"}, None
    return info, hourly


def holding_funding(
    venues: Iterable[str],
    current: Mapping[str, Mapping[str, Any] | None],
    settled: Mapping[str, Sequence[Mapping[str, Any]]],
    hold_hours: int,
    long: bool,
    now: datetime,
) -> dict[str, dict]:
    """Per venue: the rate used and `cost_bps`, the funding the position pays
    over the hold in bps of its value (negative when it is paid to the holder).
    `cost_bps` is None, with an `unavailable` reason, when the rule's data is
    missing or too old; such a venue cannot be ranked.

    `current` rows carry rate, interval_hours and ts; `settled` rows the same,
    with ts the settlement time.
    """
    if hold_hours not in HOLD_HOURS or not hold_hours:
        raise ValueError("Hold must be one of the offered periods")
    out = {}
    with localcontext() as context:
        context.prec = 40
        for venue in venues:
            if hold_hours <= CURRENT_HOLD_LIMIT_HOURS:
                info, hourly = _current(current.get(venue), now)
            else:
                info, hourly = _previous_day(settled.get(venue, ()), now)
            cost = None if hourly is None else hourly * hold_hours * 10000 * (1 if long else -1)
            out[venue] = {"unavailable": None, **info, "hold_hours": hold_hours,
                          "cost_bps": str(cost) if cost is not None else None}
    return out


def funding_unavailable(venues: Iterable[str], hold_hours: int, reason: str) -> dict[str, dict]:
    """Every venue unrankable for one reason, such as a failed read."""
    basis = "current" if hold_hours <= CURRENT_HOLD_LIMIT_HOURS else "previous_24h"
    return {venue: {"unavailable": reason, "basis": basis, "hold_hours": hold_hours, "cost_bps": None}
            for venue in venues}
