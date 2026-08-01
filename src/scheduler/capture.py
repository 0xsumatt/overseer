"""Funding-settlement capture windows — schedule derivation and the stream gate.

Only positions open AT the settlement timestamp pay or receive funding, so
there is a standing incentive to flatten just before and re-enter just after,
and the incentive scales with the rate. That distortion is invisible in the 1m
bars we already store — it blurs into one or two candles — and no REST polling
rate can reconstruct it. A bounded window of tape around each settlement is the
only way to see it.

The point of doing it windowed rather than continuously: at +/-2min around an
hourly settlement this captures about 5% of the tape, and under 1% of the day
on an 8h venue — but it is the 5% that carries the information.

Two design decisions worth not undoing
--------------------------------------
**The connection stays up; only the WRITES are gated.** Connecting on demand
would cost a TCP handshake, a WS upgrade, a subscribe round-trip and on some
venues an initial snapshot — one to three seconds, spent at exactly the moment
the run-up starts. On a two-minute pre-window that is a few percent of the
data, taken from its most valuable end. So the supervisor keeps the socket
connected permanently and this gate discards frames outside a window.

**The gate is two-level, because of a chicken-and-egg.** Deciding whether a
frame matters requires knowing its symbol, which requires parsing it. So:
`venue_open()` is a cheap pre-parse check — is ANY symbol on this venue in a
window right now — and answers the ~95% case with a single float comparison
thanks to the validity cache below. `is_open()` then filters the parsed records
per symbol. The frames we discard cost almost nothing.

Schedule derivation
-------------------
Settlement cadence is per SYMBOL, not per venue: Binance is 8h for most symbols
and 4h for some, Bybit varies, and Rise derives it per settlement record from
start/end times. Hardcoding "on the hour, plus 00/08/16 UTC" would silently
miss windows, so the schedule comes from `interval_hours` in funding_rates,
which we already store on every row for exactly this class of reason.

Settlement times are computed as a pure function of (last settlement, interval,
now) rather than tracked as a "next settlement" that has to be advanced. That
means the schedule cannot go stale between refreshes — it self-advances, and
refresh only has to notice when a venue CHANGES cadence or lists a new symbol.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("overseer.capture")

# A symbol whose newest settled funding is older than this is treated as dead
# or delisted and dropped from the schedule. Deliberately generous: capturing a
# window we did not need is free, and missing one is unrecoverable, so the
# asymmetry points hard at over-capturing.
_STALE_AFTER_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True, slots=True)
class _Cadence:
    """Everything needed to place every settlement on the timeline."""

    last_ts: float          # epoch seconds of a known settlement
    step: float             # seconds between settlements

    def nearest(self, now: float) -> float:
        """Epoch seconds of the settlement closest to `now` (before or after).

        Nearest rather than next, because a capture window straddles the
        settlement: at 30s past the hour the relevant event is the one just
        gone, not the one an hour out.
        """
        k = round((now - self.last_ts) / self.step)
        return self.last_ts + k * self.step


class SettlementCapture:
    """Schedule + gate for funding-settlement tape capture.

    Asymmetric by default: the flatten-before behaviour builds over minutes
    while re-entry is usually quicker, and you can always narrow a window in
    analysis but can never recover data you did not capture.
    """

    def __init__(
        self, *, pre_seconds: float = 120.0, post_seconds: float = 60.0
    ) -> None:
        self.pre_seconds = pre_seconds
        self.post_seconds = post_seconds
        self._cadence: dict[tuple[str, str], _Cadence] = {}
        self._by_venue: dict[str, list[_Cadence]] = {}
        # exchange -> (state, valid_from, valid_until); see venue_open()
        self._venue_cache: dict[str, tuple[bool, float, float]] = {}

    # -- schedule ------------------------------------------------------------------

    def refresh(self, rows) -> int:
        """Rebuild from `Storage.latest_funding_rows()` output.

        Rows carry (exchange, symbol, ts, rate, interval_hours); ts is the
        newest settled funding for that pair, which anchors the cadence.
        """
        cadence: dict[tuple[str, str], _Cadence] = {}
        now = datetime.now(timezone.utc).timestamp()
        for row in rows:
            try:
                hours = int(row["interval_hours"])
                ts = row["ts"]
            except (KeyError, TypeError, ValueError):
                continue
            if hours <= 0 or ts is None:
                continue
            last = ts.timestamp()
            if now - last > _STALE_AFTER_SECONDS:
                continue
            cadence[(row["exchange"], row["symbol"])] = _Cadence(last, hours * 3600.0)

        self._cadence = cadence
        by_venue: dict[str, list[_Cadence]] = {}
        for (exchange, _symbol), entry in cadence.items():
            by_venue.setdefault(exchange, []).append(entry)
        self._by_venue = by_venue
        self._venue_cache.clear()
        return len(cadence)

    async def run(self, storage, stop: asyncio.Event, *, every: float = 300.0) -> None:
        """Keep the schedule current until stopped.

        Cadence changes and new listings are rare, and the schedule
        self-advances between refreshes, so this is deliberately lazy — five
        minutes is far inside the smallest settlement interval we see (1h).
        """
        while not stop.is_set():
            try:
                count = self.refresh(await storage.latest_funding_rows())
                log.info("capture schedule: %d symbol(s) across %d venue(s)",
                         count, len(self._by_venue))
            except Exception:
                # Keep the previous schedule: it self-advances, so a failed
                # refresh degrades to "slightly stale" rather than "no capture".
                log.exception("capture schedule refresh failed; keeping previous")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=every)

    # -- the gate --------------------------------------------------------------------

    def _now(self, now: float | None) -> float:
        return datetime.now(timezone.utc).timestamp() if now is None else now

    def is_open(self, exchange: str, symbol: str, now: float | None = None) -> bool:
        """Level 2: is THIS symbol inside its capture window right now."""
        entry = self._cadence.get((exchange, symbol))
        if entry is None:
            return False
        moment = self._now(now)
        settlement = entry.nearest(moment)
        # Half-open [open, close): keeps the interval algebra in venue_open()
        # clean, and which side owns the final instant is arbitrary anyway.
        return (settlement - self.pre_seconds) <= moment < (settlement + self.post_seconds)

    def venue_open(self, exchange: str, now: float | None = None) -> bool:
        """Level 1: is ANY symbol on this venue in a window — the pre-parse check.

        Cached against the next moment the answer could change, so the common
        case (nothing open, discard the frame) is one float comparison rather
        than a scan. Windows are minutes long and settlements at least an hour
        apart, so the cache is valid for long stretches.
        """
        moment = self._now(now)
        cached = self._venue_cache.get(exchange)
        if cached is not None and cached[1] <= moment < cached[2]:
            return cached[0]

        entries = self._by_venue.get(exchange)
        if not entries:
            # Re-checked in a minute in case a refresh adds this venue.
            self._venue_cache[exchange] = (False, moment, moment + 60.0)
            return False

        # Collect every boundary at which the answer could flip — the windows
        # either side of the nearest settlement as well as its own, so the
        # validity interval is correct near a window edge. Caching a validity
        # INTERVAL rather than just an expiry keeps this a pure function of
        # `now`: with only an upper bound, a query for an earlier moment would
        # be served a later moment's answer.
        state = False
        boundaries: list[float] = []
        for entry in entries:
            settlement = entry.nearest(moment)
            for candidate in (settlement - entry.step, settlement, settlement + entry.step):
                opens = candidate - self.pre_seconds
                closes = candidate + self.post_seconds
                boundaries.append(opens)
                boundaries.append(closes)
                if opens <= moment < closes:
                    state = True

        valid_from = max((b for b in boundaries if b <= moment), default=moment - 60.0)
        valid_until = min((b for b in boundaries if b > moment), default=moment + 60.0)
        self._venue_cache[exchange] = (state, valid_from, valid_until)
        return state

    # -- introspection ----------------------------------------------------------------

    def next_settlement(self, exchange: str, symbol: str) -> datetime | None:
        """The settlement whose capture window is currently open, or the next
        one if none is — so during a window this reports the settlement being
        captured rather than skipping ahead to the following one."""
        entry = self._cadence.get((exchange, symbol))
        if entry is None:
            return None
        now = datetime.now(timezone.utc).timestamp()
        settlement = entry.nearest(now)
        if settlement + self.post_seconds < now:
            settlement += entry.step
        return datetime.fromtimestamp(settlement, tz=timezone.utc)

    @property
    def tracked(self) -> int:
        return len(self._cadence)
