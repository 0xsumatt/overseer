"""Scrape targets, loaded from a TOML config (symbols.toml).

Config (which coins) is data, not code: add a coin by editing symbols.toml, with
no .py change and no risk of a syntax error taking down the scheduler. load_targets
validates the whole file and raises with ALL problems collected, so the scheduler
fails fast and loud at startup rather than silently scraping nothing.

A target belongs to a *venue-id* (the symbols.toml section name) which maps to a
scraper via the registry — e.g. "binance_spot", "binance_perp", "hyperliquid".
Symbols are in each venue's canonical form; market_type is derived by the adapter.

A single cross-venue identifier (write "SOL" once, expand per venue) is a future
symbols.py layer — deliberately not here, since it needs declared per-venue
quote/market rules, not a string transform.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from core.enums import Timeframe
from data_collection.exchanges.registry import REGISTRY


@dataclass(frozen=True)
class ScrapeTarget:
    venue: str
    symbol: str
    interval: Timeframe
    poll_seconds: int = 60
    backfill_days: int = 2

    @property
    def job_id(self) -> str:
        return f"ohlcv:{self.venue}:{self.symbol}:{self.interval}"


@dataclass(frozen=True)
class FillsTarget:
    """A specific address whose fills we log over time (e.g. the HLP vault).

    REST userFillsByTime is best-effort for very active accounts: only ~10k most
    recent fills are exposed, so bursts during volatility are partially missed.
    Poll tight to shrink that window; the complete feed is the WS userFills
    subscription (WS phase), which writes to the same trades table.
    """

    venue: str
    address: str
    label: str
    poll_seconds: int = 30
    backfill_days: int = 1

    @property
    def job_id(self) -> str:
        return f"fills:{self.venue}:{self.label}"


def _is_eth_address(a: object) -> bool:
    return (
        isinstance(a, str)
        and a.startswith("0x")
        and len(a) == 42
        and all(c in "0123456789abcdefABCDEF" for c in a[2:])
    )


def load_targets(path: str | Path) -> tuple[list[ScrapeTarget], list[FillsTarget]]:
    """Parse + validate symbols.toml into target lists. Raises on any problem."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"symbols config not found: {p}")
    with p.open("rb") as f:
        data = tomllib.load(f)

    valid = set(REGISTRY)
    ohlcv: list[ScrapeTarget] = []
    fills: list[FillsTarget] = []
    errors: list[str] = []

    for key, section in data.items():
        if key == "fills":
            continue                       # handled separately below
        if key not in valid:
            errors.append(f"unknown venue section [{key}] (known: {sorted(valid)})")
            continue
        symbols = section.get("symbols", [])
        intervals = section.get("intervals", ["1m"])
        poll = int(section.get("poll_seconds", 60))
        if not symbols:
            errors.append(f"[{key}] has no symbols")
        tfs: list[Timeframe] = []
        for iv in intervals:
            try:
                tfs.append(Timeframe(iv))
            except ValueError:
                errors.append(f"[{key}] invalid interval {iv!r}")
        for sym in symbols:
            if not isinstance(sym, str) or not sym:
                errors.append(f"[{key}] invalid symbol {sym!r}")
                continue
            for tf in tfs:
                ohlcv.append(ScrapeTarget(key, sym, tf, poll_seconds=poll))

    for i, entry in enumerate(data.get("fills", [])):
        venue, addr, label = entry.get("venue"), entry.get("address"), entry.get("label")
        ok = True
        if venue not in valid:
            errors.append(f"fills[{i}]: unknown venue {venue!r}"); ok = False
        if not _is_eth_address(addr):
            errors.append(f"fills[{i}]: invalid address {addr!r}"); ok = False
        if not (isinstance(label, str) and label):
            errors.append(f"fills[{i}]: missing/invalid label"); ok = False
        if ok:
            fills.append(
                FillsTarget(venue, addr, label,
                            poll_seconds=int(entry.get("poll_seconds", 30)))
            )

    if errors:
        raise ValueError(
            f"invalid symbols config ({p}):\n  - " + "\n  - ".join(errors)
        )
    if not ohlcv and not fills:
        raise ValueError(f"symbols config {p} produced no targets")

    return ohlcv, fills