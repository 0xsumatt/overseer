from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("overseer.capture")

_STALE_AFTER_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True, slots=True)
class _Cadence:
    """Everything needed to place every settlement on the timeline."""

    last_ts: float          
    step: float            

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
        self._venue_cache: dict[str, tuple[bool, float, float]] = {}


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
    
                log.exception("capture schedule refresh failed; keeping previous")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=every)


    def _now(self, now: float | None) -> float:
        return datetime.now(timezone.utc).timestamp() if now is None else now

    def is_open(self, exchange: str, symbol: str, now: float | None = None) -> bool:
        entry = self._cadence.get((exchange, symbol))
        if entry is None:
            return False
        moment = self._now(now)
        settlement = entry.nearest(moment)
        return (settlement - self.pre_seconds) <= moment < (settlement + self.post_seconds)

    def venue_open(self, exchange: str, now: float | None = None) -> bool:
        moment = self._now(now)
        cached = self._venue_cache.get(exchange)
        if cached is not None and cached[1] <= moment < cached[2]:
            return cached[0]

        entries = self._by_venue.get(exchange)
        if not entries:
    
            self._venue_cache[exchange] = (False, moment, moment + 60.0)
            return False

    
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
