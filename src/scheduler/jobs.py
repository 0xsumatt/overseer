"""Scheduled jobs: scrape, record heartbeat, alert on state changes.

scrape_* do the work and return a JobOutcome (they never raise into the
scheduler). run_* wrap them: persist the heartbeat and notify Discord only on
transitions (ok->fail, fail->ok). post_digest is the once-a-day summary.

Two scrape kinds share one monitoring path via _record_and_alert:
  * scrape_ohlcv  — per (exchange, symbol, interval), resumes on bar open-time
  * scrape_fills  — per tracked address (e.g. HLP), resumes on that address's
                    last fill across all symbols
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from data_collection.base import BaseExchangeScraper
from scheduler.notify import DiscordNotifier
from scheduler.targets import FillsTarget, ScrapeTarget
from src.storage.writers import Storage

log = logging.getLogger("overseer.scheduler")


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



async def run_ohlcv(scraper, storage, notifier, state, target: ScrapeTarget) -> None:
    outcome = await scrape_ohlcv(scraper, storage, target)
    await _record_and_alert(outcome, storage, notifier, state)


async def run_fills(scraper, storage, notifier, state, target: FillsTarget) -> None:
    outcome = await scrape_fills(scraper, storage, target)
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
            f"• {r['job_id']} — last ok {r['last_success_at']}" for r in failing
        )
        await notifier.digest(
            f"⚠️ daily heartbeat — {len(failing)}/{total} jobs failing:\n{lines}"
        )