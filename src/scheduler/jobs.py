from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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


async def check_dislocations(
    storage: Storage,
    notifier: DiscordNotifier,
    state: dict[str, str],
    registry: SymbolRegistry,
    threshold_apr: float,
) -> None:
    """Ping Discord when a coin's cross-venue funding spread opens past the
    threshold. Edge-triggered like the job alerts (one ping on crossing, one on
    narrowing), with 20% hysteresis so a spread hovering at the line doesn't
    flap. State keys are 'dislocation:<asset>' — same dict as job statuses,
    disjoint keyspace."""
    try:
        rows = await storage.latest_funding_rows()
    except Exception:
        log.exception("dislocation check failed")
        return
    # a venue whose feed died still has a "latest" row; an old extreme rate is
    # not a live dislocation, so only rates settled in the last day count.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    # (exchange, apr, venue-native symbol) — the native symbol travels through
    # so the alert can deep-link straight to that venue's trade page for it,
    # not just name-drop the exchange.
    legs: dict[str, list[tuple[str, float, str]]] = {}
    for r in rows:
        if r["ts"] < cutoff:
            continue
        asset = registry.asset_for(r["symbol"]) or r["symbol"]
        apr = float(r["rate"]) * (8760 / r["interval_hours"]) * 100
        legs.setdefault(asset, []).append((r["exchange"], apr, r["symbol"]))
    for asset, venues in sorted(legs.items()):
        if len(venues) < 2:
            continue
        hi = max(venues, key=lambda v: v[1])
        lo = min(venues, key=lambda v: v[1])
        spread = hi[1] - lo[1]
        key = f"dislocation:{asset}"
        prev = state.get(key, "ok")
        if spread >= threshold_apr and prev != "wide":
            state[key] = "wide"
            await notifier.digest(
                f"📈 **{asset}** funding spread {spread:.1f}% APR — "
                f"{trade_link(hi[0], hi[2])} {hi[1]:+.1f}% vs "
                f"{trade_link(lo[0], lo[2])} {lo[1]:+.1f}%"
            )
        elif spread < threshold_apr * 0.8 and prev == "wide":
            state[key] = "ok"
            await notifier.digest(
                f"↩️ **{asset}** funding spread narrowed to {spread:.1f}% APR"
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