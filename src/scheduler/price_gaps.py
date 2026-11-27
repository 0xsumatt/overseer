"""Cross-venue price dislocations: a perpetual leaving its usual place among
the venues that list it.

Venues price the same perpetual at steady offsets from one another: their
indexes, quote currencies and funding differ (one sample had Extended's BTC
about 12 bps below Hyperliquid's, Bullet's and Phoenix's, close to Tether's
discount to USDC). So a venue is judged against its own usual offset from the
cross-venue median
(the median of that offset over the past day), not against the median itself.
It is dislocated when its bid sits above that usual place, or its ask below
it, by at least the threshold across 10 minutes of fresh quotes. Measuring
from the bid or ask means the venue's own spread is crossed: the move can be
traded, not just a wide book. Stock and commodity perps keep trading at
weekends with their reference markets shut, each venue pricing them its own
way, which is when this matters most. Posts go to the funding channel with the
other trading signals.
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from core.symbols import SymbolRegistry
from scheduler.jobs import _fmt_px
from scheduler.notify import Channel, DiscordNotifier, trade_url
from storage.writers import Storage

log = logging.getLogger("scheduler")

# How far beyond its usual offset a venue must trade, in bps, by asset family.
THRESHOLD_BPS = {"crypto": 25.0, "traditional": 50.0}
# A venue whose offset normally wanders must move further: this many times the
# median absolute deviation of its offset.
NOISE_MULTIPLE = 6.0
HISTORY = timedelta(hours=24)
MIN_HISTORY = 120               # quotes, about two hours, before a venue is judged
CONFIRM_FOR = timedelta(minutes=10)
MIN_OBSERVATIONS = 3
FRESH_FOR = timedelta(seconds=150)
MIN_VENUES = 3

Key = tuple[str, str]           # exchange, symbol


@dataclass(frozen=True)
class Quote:
    exchange: str
    symbol: str
    ts: datetime
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class Gap:
    """A venue dislocated (`opened`) or back near its usual place."""

    asset: str
    quote: Quote
    side: str                   # 'rich': bid above its usual place; 'cheap': ask below
    bps: float                  # how far beyond its usual place, from the bid or ask
    usual_bps: float            # its usual offset from the median mid
    median: float
    venues: int
    opened: bool
    unchanged_for: timedelta | None = None


@dataclass
class _Pending:
    side: str
    first: datetime
    observations: int


def _quote(row: Any) -> Quote | None:
    bid, ask = float(row["bid_price"]), float(row["ask_price"])
    if bid <= 0 or ask < bid:
        return None
    return Quote(row["exchange"], row["symbol"], row["ts"], bid, ask)


class PriceGapMonitor:
    def __init__(self, registry: SymbolRegistry) -> None:
        self._registry = registry
        self._offsets: dict[Key, deque[tuple[datetime, float]]] = defaultdict(deque)
        self._last: dict[Key, Quote] = {}
        self._unchanged_since: dict[Key, datetime] = {}
        self._pending: dict[Key, _Pending] = {}
        self._open: dict[Key, str] = {}
        self.warmed = False

    async def warm(self, storage: Storage, now: datetime) -> None:
        """Rebuild the past day's offsets from stored quotes, minute by minute,
        so a restart is not two hours blind."""
        minutes: dict[datetime, dict[Key, Quote]] = defaultdict(dict)
        rows = await storage.perp_quotes_since(now - HISTORY)
        for row in rows:
            quote = _quote(row)
            if quote is not None:
                minute = quote.ts.replace(second=0, microsecond=0)
                minutes[minute][(quote.exchange, quote.symbol)] = quote
        for minute in sorted(minutes):
            self._observe(minutes[minute].values(), minute + timedelta(minutes=1), alert=False)
        self.warmed = True
        log.info("price gaps: rebuilt from %d quotes over %d minutes", len(rows), len(minutes))

    def observe(self, quotes: Iterable[Quote], now: datetime) -> list[Gap]:
        """Take each market's newest quote; return venues that opened or closed
        a dislocation."""
        return self._observe(quotes, now, alert=True)

    def _observe(self, quotes: Iterable[Quote], now: datetime, *, alert: bool) -> list[Gap]:
        by_asset: dict[str, list[Quote]] = defaultdict(list)
        for quote in quotes:
            asset = self._registry.asset_for(quote.symbol)
            if asset is not None:
                by_asset[asset].append(quote)
        gaps: list[Gap] = []
        for asset, venues in sorted(by_asset.items()):
            if len(venues) < MIN_VENUES:
                continue
            median = statistics.median(quote.mid for quote in venues)
            family = self._registry.metadata(asset).family
            floor = THRESHOLD_BPS.get(family, THRESHOLD_BPS["crypto"])
            for quote in venues:
                gap = self._judge(asset, quote, median, len(venues), floor, now, alert)
                if gap is not None:
                    gaps.append(gap)
        return gaps

    def _judge(self, asset: str, quote: Quote, median: float, venues: int, floor: float,
               now: datetime, alert: bool) -> Gap | None:
        key = (quote.exchange, quote.symbol)
        last = self._last.get(key)
        if last is not None and quote.ts <= last.ts:
            return None                       # no new quote since the last look
        if last is None or (quote.bid, quote.ask) != (last.bid, last.ask):
            self._unchanged_since[key] = quote.ts
        self._last[key] = quote
        offsets = self._offsets[key]
        offsets.append((quote.ts, (quote.mid / median - 1) * 1e4))
        while offsets[0][0] < now - HISTORY:
            offsets.popleft()
        if not alert or len(offsets) < MIN_HISTORY:
            return None
        values = [offset for _ts, offset in offsets]
        usual = statistics.median(values)
        noise = statistics.median(abs(offset - usual) for offset in values)
        threshold = max(floor, NOISE_MULTIPLE * noise)
        beyond = {
            "rich": (quote.bid / median - 1) * 1e4 - usual,
            "cheap": usual - (quote.ask / median - 1) * 1e4,
        }
        if key in self._open:
            side = self._open[key]
            if beyond[side] >= threshold / 2:
                return None
            del self._open[key]
            return Gap(asset, quote, side, beyond[side], usual, median, venues, opened=False)
        side = max(beyond, key=beyond.__getitem__)
        if beyond[side] < threshold:
            self._pending.pop(key, None)
            return None
        pending = self._pending.get(key)
        if pending is None or pending.side != side:
            self._pending[key] = _Pending(side, quote.ts, 1)
            return None
        pending.observations += 1
        if pending.observations < MIN_OBSERVATIONS or quote.ts - pending.first < CONFIRM_FOR:
            return None
        del self._pending[key]
        self._open[key] = side
        unchanged = quote.ts - self._unchanged_since[key]
        return Gap(asset, quote, side, beyond[side], usual, median, venues, opened=True,
                   unchanged_for=unchanged if unchanged >= CONFIRM_FOR else None)

    def message(self, gap: Gap) -> str:
        quote = gap.quote
        label = self._registry.venue_identity(quote.exchange, quote.symbol).label
        url = trade_url(quote.exchange, quote.symbol)
        venue = f"[{label}]({url})" if url else f"**{label}**"
        if not gap.opened:
            return f"↩️ **{gap.asset}** {venue} back near its usual price"
        rich = gap.side == "rich"
        text = (
            f"🔀 **{gap.asset}** {venue} trades {gap.side}: its {'bid' if rich else 'ask'} "
            f"{_fmt_px(quote.bid if rich else quote.ask)} has sat {gap.bps:.0f} bps "
            f"{'above' if rich else 'below'} its usual place for "
            f"{CONFIRM_FOR.total_seconds() / 60:g}m (it usually trades {gap.usual_bps:+.0f} bps "
            f"from the {gap.venues}-venue median, now {_fmt_px(gap.median)})"
        )
        if gap.unchanged_for is not None:
            text += (f"\nIts quote has not moved for {gap.unchanged_for.total_seconds() / 60:.0f}m: "
                     "the market may be closed or its feed stale.")
        return text + "\nPrices as quoted: venues settle in USDT, USDC or USD."


async def check_price_gaps(
    storage: Storage, notifier: DiscordNotifier, monitor: PriceGapMonitor
) -> None:
    now = datetime.now(timezone.utc)
    if not monitor.warmed:
        await monitor.warm(storage, now)
    latest: dict[Key, Quote] = {}
    for row in await storage.perp_quotes_since(now - FRESH_FOR):    # oldest first
        quote = _quote(row)
        if quote is not None:
            latest[(quote.exchange, quote.symbol)] = quote
    for gap in monitor.observe(latest.values(), now):
        await notifier.digest(monitor.message(gap), Channel.FUNDING)
