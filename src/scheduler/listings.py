"""Daily listings check: what each configured venue lists.

Reports, once each, to Discord:
- a tracked asset (symbols.toml) newly listed on a venue where it is not
  configured, with the line to add. Venues name some assets differently (gold
  is XAU on some, GOLD on others), so an asset goes by its id and every name
  its configured markets carry;
- a configured market the venue no longer lists, and its return;
- on Mondays, untracked assets listed somewhere new that week and now on three
  or more exchanges: a guide to what is becoming popular.

It only reports. A ticker can name a different asset on another venue, and a
new market costs request budget, so adding one stays a config change.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from core.models import Listing
from core.symbols import VENUE_NAMES, SymbolRegistry
from data_collection.base import Capability
from scheduler.jobs import JobOutcome, _alert_venue_errors, _record_and_alert
from scheduler.notify import Channel, DiscordNotifier
from storage.writers import Storage

log = logging.getLogger("scheduler")

SPREADING_EXCHANGES = 3   # untracked assets listed on at least this many exchanges
SPREADING_DAYS = 7        # with a listing first seen within this many days
_POST_LIMIT = 1900        # Discord takes 2,000 characters a post


@dataclass(frozen=True)
class Seen:
    """A market as earlier checks left it (a venue_listings row)."""

    base: str
    first_seen: datetime | None    # None: listed before the venue's first check
    last_seen: datetime | None     # None: configured, never seen listed
    listed: bool


Row = tuple[str, str, str, datetime | None, datetime | None, bool]


@dataclass
class Findings:
    new: list[tuple[str, str, str]] = field(default_factory=list)    # venue, asset, symbol
    gone: list[tuple[str, str, str]] = field(default_factory=list)
    back: list[tuple[str, str, str]] = field(default_factory=list)
    # untracked base, exchanges listing it, exchanges where it is new this week
    spreading: list[tuple[str, list[str], list[str]]] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)                    # for venue_listings


def _asset_names(
    configured: Mapping[str, Mapping[str, str]],
    markets: Mapping[str, Sequence[Listing]],
    seen: Mapping[tuple[str, str], Seen],
) -> dict[str, str]:
    """Every name a tracked asset goes by: its id and the base of each market it
    is configured for. A name two assets share matches neither."""
    names: dict[str, str] = {}
    shared: set[str] = set()
    for venue, symbols in configured.items():
        bases = {listing.symbol: listing.base for listing in markets.get(venue, ())}
        for symbol, asset in symbols.items():
            before = seen.get((venue, symbol))
            base = bases.get(symbol) or (before.base if before else asset)
            for name in {asset.upper(), base.upper()}:
                if names.setdefault(name, asset) != asset:
                    shared.add(name)
    for name in shared:
        log.warning("listings: %s names more than one tracked asset, so matches none", name)
        del names[name]
    return names


def check(
    configured: Mapping[str, Mapping[str, str]],
    markets: Mapping[str, Sequence[Listing]],
    seen: Mapping[tuple[str, str], Seen],
    exchanges: Mapping[str, str],
    now: datetime,
    *,
    weekly: bool,
) -> Findings:
    """Compare today's listings (`markets`, venue -> listings, venues that
    answered only) with the configured markets (venue -> {symbol: asset}) and
    what earlier checks saw. `exchanges` names each venue's exchange, so a
    venue's spot and perp markets count once."""
    found = Findings()
    names = _asset_names(configured, markets, seen)
    checked = {venue for venue, _symbol in seen}
    week_ago = now - timedelta(days=SPREADING_DAYS)
    untracked: dict[str, dict[str, bool]] = defaultdict(dict)
    for venue, listings in sorted(markets.items()):
        first_check = venue not in checked
        mine = configured.get(venue, {})
        listed_now: set[str] = set()
        for listing in listings:
            base = listing.base.upper()
            listed_now.add(listing.symbol)
            before = seen.get((venue, listing.symbol))
            first_seen = before.first_seen if before else (None if first_check else now)
            found.rows.append((venue, listing.symbol, base, first_seen, now, True))
            returning = before is not None and not before.listed
            asset = mine.get(listing.symbol)
            if asset is not None:
                if returning:
                    found.back.append((venue, asset, listing.symbol))
                continue
            tracked = names.get(base)
            if tracked is not None:
                if tracked not in mine.values() and (before is None or returning):
                    found.new.append((venue, tracked, listing.symbol))
                continue
            fresh = first_seen is not None and first_seen >= week_ago
            exchange = exchanges[venue]
            untracked[base][exchange] = untracked[base].get(exchange, False) or fresh
        for symbol, asset in sorted(mine.items()):
            before = seen.get((venue, symbol))
            if symbol not in listed_now and (before is None or before.listed):
                found.gone.append((venue, asset, symbol))
                found.rows.append((venue, symbol, before.base if before else asset,
                                   before.first_seen if before else None,
                                   before.last_seen if before else None, False))
        for (seen_venue, symbol), before in seen.items():
            if (seen_venue == venue and before.listed and symbol not in listed_now
                    and symbol not in mine):
                found.rows.append((venue, symbol, before.base, before.first_seen,
                                   before.last_seen, False))
    if weekly:
        found.spreading = sorted(
            ((base, sorted(listed), sorted(e for e, fresh in listed.items() if fresh))
             for base, listed in untracked.items()
             if len(listed) >= SPREADING_EXCHANGES and any(listed.values())),
            key=lambda item: (-len(item[1]), item[0]),
        )
    return found


def posts(found: Findings, labels: Mapping[str, str]) -> list[str]:
    """Discord posts for the findings, split at line ends to fit a post."""
    lines: list[str] = []
    if found.new:
        lines.append("🆕 **Tracked assets newly listed**")
        lines += [f"• **{labels[venue]}** lists {asset} as `{symbol}`: add "
                  f"`{venue} = \"{symbol}\"` under `[assets.{asset}]`"
                  for venue, asset, symbol in found.new]
    if found.gone:
        lines.append("⚠️ **Configured markets no longer listed**")
        lines += [f"• **{labels[venue]}** `{symbol}` ({asset})" for venue, asset, symbol in found.gone]
    if found.back:
        lines.append("↩️ **Configured markets listed again**")
        lines += [f"• **{labels[venue]}** `{symbol}` ({asset})" for venue, asset, symbol in found.back]
    if found.spreading:
        lines.append(f"📈 **Untracked assets spreading**: listed somewhere new this week, "
                     f"now on {SPREADING_EXCHANGES}+ exchanges")
        lines += [f"• {base}: {len(listed)} ({', '.join(listed)}), new on {', '.join(fresh)}"
                  for base, listed, fresh in found.spreading]
    out: list[str] = []
    for line in lines:
        if out and len(out[-1]) + 1 + len(line) <= _POST_LIMIT:
            out[-1] += "\n" + line
        else:
            out.append(line)
    return out


def _exchange(scraper: Any) -> str:
    return VENUE_NAMES.get(scraper.exchange.value, scraper.exchange.value)


def _label(venue: str, scraper: Any) -> str:
    """'Binance perp', 'Hyperliquid xyz', 'BULK'."""
    qualifier = venue.removeprefix(scraper.exchange.value).strip("_")
    return f"{_exchange(scraper)} {qualifier}".strip()


async def run_listings(
    scrapers: Mapping[str, Any], storage: Storage, notifier: DiscordNotifier,
    state: dict[str, str], registry: SymbolRegistry,
) -> None:
    now = datetime.now(timezone.utc)
    venues = sorted(v for v, s in scrapers.items() if Capability.MARKETS in s.capabilities)
    markets: dict[str, Sequence[Listing]] = {}
    errors: dict[str, str] = {}
    for venue in venues:
        try:
            markets[venue] = await scrapers[venue].fetch_markets()
        except Exception as exc:
            log.exception("listings fetch failed: %s", venue)
            errors[venue] = repr(exc)
    _alert_venue_errors("listings", venues, errors, notifier, state)
    configured: dict[str, dict[str, str]] = defaultdict(dict)
    for asset in registry.assets():
        for venue, symbol in registry.listings(asset).items():
            if venue in scrapers:
                configured[venue][symbol] = asset
    fetched = sum(len(listings) for listings in markets.values())
    try:
        seen = {(r["venue"], r["symbol"]): Seen(r["base"], r["first_seen"], r["last_seen"], r["listed"])
                for r in await storage.venue_listings()}
        found = check(configured, markets, seen, {v: _exchange(scrapers[v]) for v in venues},
                      now, weekly=now.weekday() == 0)
        await storage.write_venue_listings(found.rows)
    except Exception as exc:
        log.exception("listings check failed")
        _record_and_alert(JobOutcome("listings:daily", "fail", fetched, 0, repr(exc), now),
                          storage, notifier, state)
        return
    for post in posts(found, {v: _label(v, scrapers[v]) for v in venues}):
        await notifier.digest(post, Channel.LISTINGS)
    detail = "; ".join(f"{venue}: {error}" for venue, error in errors.items())
    if errors and not markets:
        outcome = JobOutcome("listings:daily", "fail", 0, 0, detail, now)
    else:
        outcome = JobOutcome("listings:daily", "ok", fetched, len(found.new),
                             f"partial: {detail}" if detail else None, now)
    _record_and_alert(outcome, storage, notifier, state)
