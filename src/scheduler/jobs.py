from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from core.enums import MarketType, QuoteCurrency
from core.models import TopOfBook
from core.symbols import SymbolRegistry
from data_collection.base import BaseExchangeScraper, Capability
from scheduler.notify import DiscordNotifier, trade_link
from scheduler.targets import FillsTarget, FundingTarget, LiquidityTarget, ScrapeTarget
from storage.writers import Storage

log = logging.getLogger("scheduler")


@dataclass(frozen=True)
class JobOutcome:
    job_id: str
    status: str            # 'ok' | 'fail'
    fetched: int
    new_rows: int
    error: str | None
    ran_at: datetime


# -- scrape functions (return an outcome; never raise into the scheduler) -----

async def scrape_ohlcv(
    scraper: BaseExchangeScraper, storage: Storage, target: ScrapeTarget
) -> JobOutcome:
    ran_at = datetime.now(timezone.utc)
    try:
        market_type = scraper.market_type_for(target.symbol)
        latest = await storage.latest_ohlcv_ts(
            scraper.exchange, market_type, target.symbol, target.interval
        )
        since = latest or (ran_at - timedelta(days=target.backfill_days))
        records = await scraper.fetch_ohlcv(target.symbol, target.interval, since)
        new_rows = await storage.write_ohlcv(records)
    except Exception as exc:
        log.exception("scrape failed: %s", target.job_id)
        return JobOutcome(target.job_id, "fail", 0, 0, repr(exc), ran_at)
    return JobOutcome(target.job_id, "ok", len(records), new_rows, None, ran_at)


async def scrape_fills(
    scraper: BaseExchangeScraper, storage: Storage, target: FillsTarget
) -> JobOutcome:
    ran_at = datetime.now(timezone.utc)
    try:
        latest = await storage.latest_fill_ts(scraper.exchange, target.address)
        since = latest or (ran_at - timedelta(days=target.backfill_days))
        fills = await scraper.fetch_fills(target.address, since)
        new_rows = await storage.write_trades(fills)
    except Exception as exc:
        log.exception("fills scrape failed: %s", target.job_id)
        return JobOutcome(target.job_id, "fail", 0, 0, repr(exc), ran_at)
    return JobOutcome(target.job_id, "ok", len(fills), new_rows, None, ran_at)


# -- monitoring: heartbeat + edge-triggered alerts ----------------------------

async def _handle_transition(
    outcome: JobOutcome, state: dict[str, str], notifier: DiscordNotifier
) -> None:
    prev = state.get(outcome.job_id)
    state[outcome.job_id] = outcome.status
    if outcome.status == "fail" and prev != "fail":
        await notifier.failure(outcome.job_id, outcome.error)
    elif outcome.status == "ok" and prev == "fail":
        await notifier.recovery(outcome.job_id, outcome.fetched, outcome.new_rows)


async def _record_and_alert(
    outcome: JobOutcome, storage: Storage, notifier: DiscordNotifier, state: dict[str, str]
) -> None:
    try:
        await storage.record_job_run(
            outcome.job_id, outcome.status, outcome.fetched,
            outcome.new_rows, outcome.error, outcome.ran_at,
        )
    except Exception:
        log.exception("heartbeat write failed: %s", outcome.job_id)
    await _handle_transition(outcome, state, notifier)
    if outcome.status == "ok":
        log.info("%s  fetched=%d new=%d", outcome.job_id, outcome.fetched, outcome.new_rows)


# -- registered jobs (thin wrappers; explicit so no function is passed as a job arg)

async def run_ohlcv(scraper, storage, notifier, state, target: ScrapeTarget) -> None:
    outcome = await scrape_ohlcv(scraper, storage, target)
    await _record_and_alert(outcome, storage, notifier, state)


async def run_fills(scraper, storage, notifier, state, target: FillsTarget) -> None:
    outcome = await scrape_fills(scraper, storage, target)
    await _record_and_alert(outcome, storage, notifier, state)




async def scrape_funding(
    scraper: BaseExchangeScraper, storage: Storage, target: FundingTarget
) -> JobOutcome:
    ran_at = datetime.now(timezone.utc)
    try:
        latest = await storage.latest_funding_ts(scraper.exchange, target.symbol)
        since = latest or (ran_at - timedelta(days=target.backfill_days))
        records = await scraper.fetch_funding(target.symbol, since)
        new_rows = await storage.write_funding(records)
    except Exception as exc:
        log.exception("funding scrape failed: %s", target.job_id)
        return JobOutcome(target.job_id, "fail", 0, 0, repr(exc), ran_at)
    return JobOutcome(target.job_id, "ok", len(records), new_rows, None, ran_at)


async def scrape_liquidity(
    scraper: BaseExchangeScraper, storage: Storage, target: LiquidityTarget
) -> JobOutcome:
    ran_at = datetime.now(timezone.utc)
    try:
        records = await scraper.fetch_liquidity(list(target.symbols))
        new_rows = await storage.write_liquidity(records)
    except Exception as exc:
        log.exception("liquidity scrape failed: %s", target.job_id)
        return JobOutcome(target.job_id, "fail", 0, 0, repr(exc), ran_at)
    return JobOutcome(target.job_id, "ok", len(records), new_rows, None, ran_at)


async def run_funding(scraper, storage, notifier, state, target: FundingTarget) -> None:
    outcome = await scrape_funding(scraper, storage, target)
    await _record_and_alert(outcome, storage, notifier, state)


async def run_liquidity(scraper, storage, notifier, state, target: LiquidityTarget) -> None:
    outcome = await scrape_liquidity(scraper, storage, target)
    await _record_and_alert(outcome, storage, notifier, state)


async def _alert_venue_volume_errors(
    scrapers: dict, errors: dict[str, str],
    notifier: DiscordNotifier, state: dict[str, str],
) -> None:
    """Per-venue edge-triggered alerts for the sweep, independent of the
    aggregate job status. Without this, one venue erroring inside an otherwise-
    successful sweep only ever shows up as a 'partial:' note buried in
    job_runs — never a Discord ping — so a broken venue could go unnoticed
    indefinitely. Reuses notifier.failure/recovery (same shape as the polling
    job alerts) with a synthetic job_id so it reads consistently in Discord."""
    candidates = [v for v, s in scrapers.items() if Capability.VENUE_VOLUME in s.capabilities]
    for venue in candidates:
        key = f"venue_volume:{venue}"
        prev = state.get(key, "ok")
        if venue in errors and prev != "fail":
            state[key] = "fail"
            await notifier.failure(key, errors[venue])
        elif venue not in errors and prev == "fail":
            state[key] = "ok"
            await notifier.recovery(key, 1, 0)


async def scrape_venue_volume(
    scrapers: dict, storage: Storage, notifier: DiscordNotifier, state: dict[str, str]
) -> JobOutcome:
    """Daily venue-wide volume sweep — ONE job, sequential across venues (each
    call rides its venue's own limiter, so it can't collide with the polling
    herd), legs merged per exchange (binance spot+perp venues -> one row)."""
    from core.models import VenueVolume

    ran_at = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    legs: dict = {}          # exchange -> {"spot": x|None, "perp": y|None}
    errors: dict[str, str] = {}     # venue-id -> error, for per-venue alerting
    fetched = 0
    for venue, scraper in sorted(scrapers.items()):
        if Capability.VENUE_VOLUME not in scraper.capabilities:
            continue
        try:
            vol = await scraper.fetch_venue_volume()
            fetched += 1
        except Exception as exc:
            errors[venue] = repr(exc)
            continue
        agg = legs.setdefault(scraper.exchange, {"spot": None, "perp": None})
        for side in ("spot", "perp"):
            v = vol.get(side)
            if v is not None:
                agg[side] = (agg[side] or 0) + v

    await _alert_venue_volume_errors(scrapers, errors, notifier, state)

    records = [
        VenueVolume(
            exchange=ex, ts=ran_at,
            volume_total=(v["spot"] or 0) + (v["perp"] or 0),
            volume_spot=v["spot"], volume_perp=v["perp"],
        )
        for ex, v in legs.items()
    ]
    try:
        new_rows = await storage.write_venue_volume(records) if records else 0
    except Exception as exc:
        return JobOutcome("venue_volume:daily", "fail", fetched, 0, repr(exc), ran_at)
    if errors and not records:
        return JobOutcome("venue_volume:daily", "fail", fetched, 0,
                          "; ".join(f"{v}: {e}" for v, e in errors.items()), ran_at)
    # partial failures record as ok-with-error-note: some venues > no venues.
    # The per-venue alert above is what actually notifies; this note is just
    # the job_runs/health-page detail.
    err = ("partial: " + "; ".join(f"{v}: {e}" for v, e in errors.items())) if errors else None
    return JobOutcome("venue_volume:daily", "ok", fetched, new_rows, err, ran_at)


async def run_venue_volume(scrapers: dict, storage, notifier, state) -> None:
    outcome = await scrape_venue_volume(scrapers, storage, notifier, state)
    await _record_and_alert(outcome, storage, notifier, state)


def _fmt_px(value: Decimal) -> str:
    """Price at a readable precision across large and sub-dollar markets."""
    if value >= 1000:
        return f"{value:,.1f}"
    if value >= 1:
        return f"{value:,.3f}"
    return f"{value:.6f}"


def _fmt_size(value: Decimal) -> str:
    if value >= 1000:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:,.3f}"
    return f"{value:.6f}"


@dataclass(frozen=True)
class FundingLeg:
    exchange: str
    venue: str | None
    symbol: str
    apr: float


class UsdcUsdtQuoteCache:
    """One shared, side-aware USDC/USDT quote for event-driven alerts."""

    def __init__(
        self,
        binance_spot: BaseExchangeScraper | None,
        *,
        fresh_for: timedelta = timedelta(minutes=1),
        fallback_for: timedelta = timedelta(minutes=5),
    ) -> None:
        self._scraper = binance_spot
        self._fresh_for = fresh_for
        self._fallback_for = fallback_for
        self._book: TopOfBook | None = None

    async def get(self) -> tuple[TopOfBook, bool]:
        now = datetime.now(timezone.utc)
        if self._book is not None and now - self._book.ts <= self._fresh_for:
            return self._book, False
        try:
            if self._scraper is None or Capability.BBO not in self._scraper.capabilities:
                raise RuntimeError("binance spot BBO is unavailable")
            self._book = await asyncio.wait_for(
                self._scraper.fetch_bbo("USDC/USDT"), timeout=5
            )
            return self._book, False
        except Exception:
            if self._book is not None and now - self._book.ts <= self._fallback_for:
                log.warning("USDC/USDT refresh failed; using cached quote", exc_info=True)
                return self._book, True
            raise


def _venue_for_leg(
    registry: SymbolRegistry,
    scrapers: dict[str, BaseExchangeScraper],
    asset: str,
    exchange: str,
    symbol: str,
) -> str | None:
    for venue, configured_symbol in registry.listings(asset).items():
        scraper = scrapers.get(venue)
        if (
            scraper is not None
            and configured_symbol == symbol
            and scraper.exchange.value == exchange
            and scraper.market_type_for(symbol) is MarketType.PERP
        ):
            return venue
    return None


async def _safe_bbo(
    leg: FundingLeg, scrapers: dict[str, BaseExchangeScraper]
) -> TopOfBook | None:
    scraper = scrapers.get(leg.venue or "")
    if scraper is None or Capability.BBO not in scraper.capabilities:
        log.warning("no BBO adapter for %s %s", leg.exchange, leg.symbol)
        return None
    try:
        book = await asyncio.wait_for(scraper.fetch_bbo(leg.symbol), timeout=5)
        if (
            book.bid_price <= 0
            or book.ask_price <= 0
            or book.bid_size <= 0
            or book.ask_size <= 0
            or book.bid_price > book.ask_price
        ):
            raise ValueError(f"invalid BBO: {book!r}")
        return book
    except Exception:
        log.warning("BBO fetch failed for %s %s", leg.exchange, leg.symbol, exc_info=True)
        return None

async def _safe_fx(
    cache: UsdcUsdtQuoteCache,
) -> tuple[TopOfBook | None, bool]:
    try:
        return await cache.get()
    except Exception:
        log.warning("USDC/USDT quote unavailable", exc_info=True)
        return None, False


def _usdt_prices(
    book: TopOfBook, fx: TopOfBook | None
) -> tuple[Decimal, Decimal, str] | None:
    if book.quote_currency is QuoteCurrency.USDT:
        return book.bid_price, book.ask_price, QuoteCurrency.USDT.value
    if fx is None:
        return None
    return (
        book.bid_price * fx.bid_price,
        book.ask_price * fx.ask_price,
        QuoteCurrency.USDT.value,
    )


def _book_line(
    side: str,
    leg: FundingLeg,
    book: TopOfBook | None,
    fx: TopOfBook | None,
) -> str:
    link = trade_link(leg.exchange, leg.symbol)
    header = f"**{side}** {link} {leg.apr:+.1f}% APR"
    if book is None:
        return header + "\nbook unavailable"
    converted = _usdt_prices(book, fx)
    if converted is None:
        bid, ask, quote = book.bid_price, book.ask_price, book.quote_currency.value
    else:
        bid, ask, quote = converted
    return (
        f"{header}\n"
        f"bid {_fmt_px(bid)} × {_fmt_size(book.bid_size)} | "
        f"ask {_fmt_px(ask)} × {_fmt_size(book.ask_size)} {quote}"
    )


async def _execution_details(
    hi: FundingLeg,
    lo: FundingLeg,
    scrapers: dict[str, BaseExchangeScraper],
    fx_cache: UsdcUsdtQuoteCache,
) -> str:
    hi_task = asyncio.create_task(_safe_bbo(hi, scrapers))
    lo_task = asyncio.create_task(_safe_bbo(lo, scrapers))
    needs_fx = any(
        (
            scraper := scrapers.get(leg.venue or "")
        ) is not None and getattr(
            scraper, "quote_currency", None
        ) is QuoteCurrency.USDC
        for leg in (hi, lo)
    )
    if needs_fx:
        hi_book, lo_book, fx_result = await asyncio.gather(
            hi_task, lo_task, asyncio.create_task(_safe_fx(fx_cache))
        )
        fx, fx_cached = fx_result
    else:
        hi_book, lo_book = await asyncio.gather(hi_task, lo_task)
        fx, fx_cached = None, False

    lines = [
        _book_line("SHORT", hi, hi_book, fx),
        _book_line("LONG", lo, lo_book, fx),
    ]
    hi_usdt = _usdt_prices(hi_book, fx) if hi_book is not None else None
    lo_usdt = _usdt_prices(lo_book, fx) if lo_book is not None else None
    if fx is not None:
        cached = " · cached" if fx_cached else ""
        lines.append(
            f"USDC/USDT {fx.bid_price:.6f} / {fx.ask_price:.6f}{cached}"
        )
    if hi_usdt is not None and lo_usdt is not None:
        short_bid, long_ask = hi_usdt[0], lo_usdt[1]
        mid = (short_bid + long_ask) / 2
        if mid > 0:
            edge_bps = (short_bid - long_ask) / mid * Decimal(10_000)
            label = "entry edge" if edge_bps >= 0 else "entry cost"
            lines.append(f"Gross {label}: {edge_bps:+.1f} bps")
    books = [book for book in (hi_book, lo_book) if book is not None]
    if books:
        captured = max(book.ts for book in books)
        lines.append(f"Books captured {captured:%H:%M:%S} UTC")
    lines.append("Fees and slippage beyond displayed size excluded")
    return "\n\n" + "\n\n".join(lines)


async def check_dislocations(
    storage: Storage,
    notifier: DiscordNotifier,
    state: dict[str, str],
    registry: SymbolRegistry,
    threshold_apr: float,
    scrapers: dict[str, BaseExchangeScraper],
    fx_cache: UsdcUsdtQuoteCache,
) -> None:
    """Alert on fresh current funding, enriching new crossings with live BBOs."""
    try:
        rows = await storage.latest_current_funding_rows()
    except Exception:
        log.exception("current funding dislocation check failed")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    legs: dict[str, list[FundingLeg]] = {}
    for row in rows:
        if row["ts"] < cutoff:
            continue
        asset = registry.asset_for(row["symbol"]) or row["symbol"]
        apr = float(row["rate"]) * (8760 / row["interval_hours"]) * 100
        venue = _venue_for_leg(
            registry, scrapers, asset, row["exchange"], row["symbol"]
        )
        legs.setdefault(asset, []).append(
            FundingLeg(row["exchange"], venue, row["symbol"], apr)
        )

    for asset, venues in sorted(legs.items()):
        if len(venues) < 2:
            continue
        hi = max(venues, key=lambda leg: leg.apr)
        lo = min(venues, key=lambda leg: leg.apr)
        spread = hi.apr - lo.apr
        key = f"dislocation:{asset}"
        prev = state.get(key, "ok")
        if spread >= threshold_apr and prev != "wide":
            state[key] = "wide"
            execution = await _execution_details(hi, lo, scrapers, fx_cache)
            await notifier.digest(
                f"📈 **{asset}** current funding spread {spread:.1f}% APR"
                + execution
            )
        elif spread < threshold_apr * 0.8 and prev == "wide":
            state[key] = "ok"
            await notifier.digest(
                f"↩️ **{asset}** current funding spread narrowed to {spread:.1f}% APR"
            )


async def post_digest(storage: Storage, notifier: DiscordNotifier) -> None:
    """Once-a-day heartbeat-to-Discord: all healthy, or who's failing."""
    rows = await storage.all_job_runs()
    total = len(rows)
    failing = [r for r in rows if r["last_status"] == "fail"]
    if not failing:
        ingested = sum(r["new_rows"] for r in rows)
        await notifier.digest(
            f"🟢 daily heartbeat — all {total} jobs healthy "
            f"({ingested} new rows on last run)"
        )
    else:
        lines = "\n".join(
            f"• {r['job_id']} — last ok {r['last_success_at'] or 'never'}"
            for r in failing
        )
        await notifier.digest(
            f"⚠️ daily heartbeat — {len(failing)}/{total} jobs failing:\n{lines}"
        )