
from __future__ import annotations

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.config import settings
from data_collection.exchanges.registry import REGISTRY
from scheduler.jobs import post_digest, run_fills, run_funding, run_liquidity, run_ohlcv
from scheduler.notify import DiscordNotifier
from scheduler.targets import load_targets
from storage.writers import Storage

log = logging.getLogger("overseer.scheduler")

def _jitter(poll_seconds: int) -> int:

    return max(2, min(15, poll_seconds // 6))

def build_scheduler(
    scrapers, storage, notifier, state,
    ohlcv_targets, fills_targets, funding_targets, liquidity_targets,
) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(
        job_defaults={
            "coalesce": True,          # collapse missed runs into one on resume
            "misfire_grace_time": 30,  # tolerate a late start without skipping
            "max_instances": 1,        # never overlap a slow run with the next
        }
    )
    # all four target kinds schedule identically: interval-poll, one job per target
    for run, targets in (
        (run_ohlcv, ohlcv_targets),
        (run_fills, fills_targets),
        (run_funding, funding_targets),
        (run_liquidity, liquidity_targets),
    ):
        for t in targets:
            sched.add_job(
                run,
                trigger=IntervalTrigger(seconds=t.poll_seconds),
                args=[scrapers[t.venue], storage, notifier, state, t],
                id=t.job_id, name=t.job_id, replace_existing=True,
            )
    # once-a-day heartbeat-to-Discord (no-op if no webhook configured)
    sched.add_job(
        post_digest, trigger=CronTrigger(hour=8, minute=0, timezone="UTC"),
        args=[storage, notifier], id="digest:daily", name="digest:daily",
        replace_existing=True,
    )
    return sched


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    storage = Storage(settings.database_url)
    await storage.connect()

    notifier = DiscordNotifier(settings.discord_webhook_url)
    log.info("discord notifications: %s", "on" if notifier.enabled else "off")

    # Load + validate the scrape config. Any error raises here and the process
    # exits loudly rather than starting up scraping nothing.
    ohlcv_targets, fills_targets, funding_targets, liquidity_targets = load_targets(
        settings.symbols_file
    )
    log.info("loaded %d ohlcv + %d fills + %d funding + %d liquidity targets from %s",
             len(ohlcv_targets), len(fills_targets), len(funding_targets),
             len(liquidity_targets), settings.symbols_file)

    # one scraper instance per venue, shared across every target kind.
    venues = {
        t.venue
        for targets in (ohlcv_targets, fills_targets, funding_targets, liquidity_targets)
        for t in targets
    }
    scrapers = {v: REGISTRY[v]() for v in venues}

    state: dict[str, str] = {}      # job_id -> last status, for edge-triggered alerts
    sched = build_scheduler(scrapers, storage, notifier, state,
                            ohlcv_targets, fills_targets, funding_targets, liquidity_targets)
    sched.start()
    log.info("scheduler started with %d job(s)", len(sched.get_jobs()))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        log.info("shutting down…")
        sched.shutdown(wait=False)
        for s in scrapers.values():
            await s.aclose()
        await notifier.aclose()
        await storage.close()


def cli() -> None:
    """Console-script entrypoint — sync wrapper around the async main()."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()