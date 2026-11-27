from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.symbols import AssetMetadata
from scheduler.price_gaps import MIN_HISTORY, PriceGapMonitor, Quote

T0 = datetime(2026, 11, 28, tzinfo=timezone.utc)
# Steady offsets from the median, in bps: these are each venue's usual place.
USUAL = {"binance": 0.0, "hyperliquid": 2.0, "extended": -12.0, "phoenix": 15.0}


class Registry:
    def asset_for(self, symbol: str) -> str | None:
        return "BTC" if symbol.startswith("BTC") else None

    def metadata(self, asset: str) -> AssetMetadata:
        return AssetMetadata("crypto", "crypto", "majors", asset)


def _quotes(minute: int, *, moved: dict[str, float] | None = None,
            frozen: dict[str, Quote] | None = None) -> list[Quote]:
    """Every venue at its usual offset from a slowly drifting price, one bp
    either side of its mid, except venues `moved` by some bps or `frozen`."""
    ts = T0 + timedelta(minutes=minute)
    base = 60_000 * (1 + minute * 1e-6)
    out = []
    for exchange, offset in USUAL.items():
        if frozen and exchange in frozen:
            q = frozen[exchange]
            out.append(Quote(q.exchange, q.symbol, ts, q.bid, q.ask))
            continue
        mid = base * (1 + (offset + (moved or {}).get(exchange, 0.0)) / 1e4)
        out.append(Quote(exchange, "BTC", ts, mid * (1 - 1e-4), mid * (1 + 1e-4)))
    return out


def _run(monitor: PriceGapMonitor, minutes: range, **kwargs) -> list[tuple[int, object]]:
    gaps = []
    for minute in minutes:
        for gap in monitor.observe(_quotes(minute, **kwargs), T0 + timedelta(minutes=minute)):
            gaps.append((minute, gap))
    return gaps


def test_venues_at_their_usual_offsets_never_alert_however_far_apart() -> None:
    # Extended and Phoenix sit 27 bps apart all day: that is their normal.
    assert _run(PriceGapMonitor(Registry()), range(300)) == []  # type: ignore[arg-type]


def test_a_venue_off_its_usual_place_alerts_once_after_ten_minutes_then_once_back() -> None:
    monitor = PriceGapMonitor(Registry())  # type: ignore[arg-type]
    _run(monitor, range(MIN_HISTORY + 10))
    start = MIN_HISTORY + 10
    # Extended's bid 40 bps above where it usually trades, for 20 minutes.
    gaps = _run(monitor, range(start, start + 20), moved={"extended": 42.0})
    assert [(minute - start, gap.quote.exchange, gap.side, gap.opened) for minute, gap in gaps] == [
        (10, "extended", "rich", True),
    ]
    # Moving from lowest to highest also lifts the four-venue median, so less
    # than the 42 bps move shows; it is still past the 25 bps threshold.
    assert 25 < gaps[0][1].bps < 42
    assert gaps[0][1].usual_bps == pytest.approx(-13, abs=0.5)

    back = _run(monitor, range(start + 20, start + 22))
    assert [(gap.quote.exchange, gap.opened) for _minute, gap in back] == [("extended", False)]


def test_a_venue_left_behind_by_a_frozen_quote_says_so() -> None:
    monitor = PriceGapMonitor(Registry())  # type: ignore[arg-type]
    _run(monitor, range(MIN_HISTORY + 10))
    start = MIN_HISTORY + 10
    stuck = next(q for q in _quotes(start - 1) if q.exchange == "phoenix")
    # Everyone else moves 60 bps up; Phoenix's quote stays where it was.
    gaps = _run(monitor, range(start, start + 12),
                moved={venue: 60.0 for venue in USUAL}, frozen={"phoenix": stuck})
    [(_minute, gap)] = gaps
    assert (gap.quote.exchange, gap.side, gap.opened) == ("phoenix", "cheap", True)
    assert gap.unchanged_for is not None and gap.unchanged_for >= timedelta(minutes=10)


class QuoteStorage:
    def __init__(self, quotes: list[Quote]) -> None:
        self.quotes = quotes

    async def perp_quotes_since(self, since: datetime) -> list[dict[str, object]]:
        return [{"exchange": q.exchange, "symbol": q.symbol, "ts": q.ts,
                 "bid_price": q.bid, "ask_price": q.ask}
                for q in self.quotes if q.ts >= since]


@pytest.mark.asyncio
async def test_a_restart_rebuilds_usual_places_from_stored_quotes() -> None:
    start = MIN_HISTORY + 10
    stored = [q for minute in range(start) for q in _quotes(minute)]
    monitor = PriceGapMonitor(Registry())  # type: ignore[arg-type]
    await monitor.warm(QuoteStorage(stored), T0 + timedelta(minutes=start))  # type: ignore[arg-type]
    gaps = _run(monitor, range(start, start + 11), moved={"hyperliquid": -45.0})
    assert [(minute - start, gap.quote.exchange, gap.side) for minute, gap in gaps] == [
        (10, "hyperliquid", "cheap"),
    ]
