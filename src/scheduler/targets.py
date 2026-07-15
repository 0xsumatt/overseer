from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from core.enums import MarketType, Timeframe
from core.symbols import SymbolConfigError, SymbolRegistry
from data_collection.base import Capability
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
class FundingTarget:
    """Settled funding history for one perp symbol. Resume-based, so polling
    more often than settlements just returns nothing new — cheap."""
    venue: str
    symbol: str
    poll_seconds: int = 900
    backfill_days: int = 14

    @property
    def job_id(self) -> str:
        return f"funding:{self.venue}:{self.symbol}"


@dataclass(frozen=True)
class LiquidityTarget:
    """Point-in-time OI/volume snapshots for a batch of perp symbols on one
    venue. Batched because HL serves all coins in one call."""
    venue: str
    symbols: tuple[str, ...]
    poll_seconds: int = 300

    @property
    def job_id(self) -> str:
        return f"liquidity:{self.venue}"


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


def load_targets(path: str | Path) -> tuple[
    list[ScrapeTarget], list[FillsTarget], list[FundingTarget], list[LiquidityTarget]
]:
    """Parse + validate symbols.toml ([venues] + [assets] format) into target
    lists. Raises with ALL problems collected, so the scheduler fails fast and
    loud at startup rather than silently scraping nothing.

    Targets are the cross product of [assets.*] listings and [venues.*]
    settings: an asset listed on a venue gets an OHLCV target per interval;
    venues flagged funding/liquidity get those jobs for their PERP listings
    only (spot symbols are filtered via the adapter's market_type_for).
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"symbols config not found: {p}")
    with p.open("rb") as f:
        data = tomllib.load(f)

    errors: list[str] = []

    # -- the asset map (cross-venue symbol registry) ---------------------------
    try:
        registry = SymbolRegistry.from_config(data)
    except SymbolConfigError as exc:
        raise ValueError(str(exc)) from exc

    # -- venue settings ---------------------------------------------------------
    venues_cfg = data.get("venues", {})
    known = set(REGISTRY)
    for venue in venues_cfg:
        if venue not in known:
            errors.append(f"unknown venue [venues.{venue}] (known: {sorted(known)})")
    # assets may only reference declared venues
    for asset in registry.assets():
        for venue in registry.listings(asset):
            if venue not in venues_cfg:
                errors.append(
                    f"[assets.{asset}] references venue {venue!r} "
                    "with no [venues.{0}] section".format(venue)
                )

    ohlcv: list[ScrapeTarget] = []
    funding: list[FundingTarget] = []
    liquidity: list[LiquidityTarget] = []

    for venue, section in venues_cfg.items():
        if venue not in known:
            continue
        symbols = registry.venue_symbols(venue)
        if not symbols:
            errors.append(f"[venues.{venue}] has no assets listing it")
            continue
        poll = int(section.get("poll_seconds", 60))
        tfs: list[Timeframe] = []
        for iv in section.get("intervals", ["1m"]):
            try:
                tfs.append(Timeframe(iv))
            except ValueError:
                errors.append(f"[venues.{venue}] invalid interval {iv!r}")
        for sym in symbols:
            for tf in tfs:
                ohlcv.append(ScrapeTarget(venue, sym, tf, poll_seconds=poll))

        # funding / OI are PERP-domain; filter via the adapter so a spot
        # listing can never spawn a funding job.
        perp_syms = [
            sym for sym in symbols
            if REGISTRY[venue].market_type_for(sym) == MarketType.PERP
        ]
        if section.get("funding"):
            if Capability.FUNDING not in REGISTRY[venue].capabilities:
                errors.append(
                    f"[venues.{venue}] has funding = true but its adapter does not "
                    "implement fetch_funding — sync gap? (check the adapter file)"
                )
            else:
                for sym in perp_syms:
                    funding.append(FundingTarget(venue, sym))
        if section.get("liquidity") and perp_syms:
            if Capability.LIQUIDITY not in REGISTRY[venue].capabilities:
                errors.append(
                    f"[venues.{venue}] has liquidity = true but its adapter does not "
                    "implement fetch_liquidity — sync gap? (check the adapter file)"
                )
            else:
                liquidity.append(LiquidityTarget(venue, tuple(perp_syms)))

    # -- tracked addresses -------------------------------------------------------
    fills: list[FillsTarget] = []
    for i, entry in enumerate(data.get("fills", [])):
        venue, addr, label = entry.get("venue"), entry.get("address"), entry.get("label")
        ok = True
        if venue not in known:
            errors.append(f"fills[{i}]: unknown venue {venue!r}"); ok = False
        elif not REGISTRY[venue].is_fill_ref(addr):
            errors.append(f"fills[{i}]: invalid account ref {addr!r} for venue {venue}"); ok = False
        if not (isinstance(label, str) and label):
            errors.append(f"fills[{i}]: missing/invalid label"); ok = False
        if ok:
            fills.append(FillsTarget(venue, addr, label,
                                     poll_seconds=int(entry.get("poll_seconds", 30))))

    if errors:
        raise ValueError(f"invalid symbols config ({p}):\n  - " + "\n  - ".join(errors))
    if not ohlcv and not fills:
        raise ValueError(f"symbols config {p} produced no targets")

    return ohlcv, fills, funding, liquidity