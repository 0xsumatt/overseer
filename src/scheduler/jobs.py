from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from data_collection.base import BaseExchangeScraper
from scheduler.notify import DiscordNotifier
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