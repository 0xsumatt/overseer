
from __future__ import annotations

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.config import settings
from core.symbols import SymbolRegistry
from data_collection.exchanges.registry import REGISTRY, STREAM_REGISTRY
from scheduler.capture import SettlementCapture
from scheduler.jobs import (
    UsdcUsdtQuoteCache,
    check_dislocations,
    post_digest,
    run_fills,
    run_funding,
    run_liquidity,
    run_ohlcv,
    run_venue_volume,
)
from scheduler.notify import DiscordNotifier
from scheduler.streams import run_stream
from scheduler.targets import load_stream_targets, load_targets
from storage.writers import Storage

log = logging.getLogger("overseer.scheduler")

def _jitter(poll_seconds: int) -> int:

    return max(2, min(15, poll_seconds // 6))

def build_scheduler(
    scrapers, storage, notifier, state,
    ohlcv_targets, fills_targets, funding_targets, liquidity_targets,
    registry: SymbolRegistry | None = None,
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
    # venue-wide 24h volume sweep — once daily, sequential across every venue
    # that declares the capability (see scrape_venue_volume). 00:37 UTC: off
    # the top of the hour, matching the dashboard's "first sweep" copy.
    sched.add_job(
        run_venue_volume, trigger=CronTrigger(hour=0, minute=37, timezone="UTC"),
        args=[scrapers, storage, notifier, state],
        id="venue_volume:daily", name="venue_volume:daily", replace_existing=True,
    )
    # Current-funding dislocations are checked against the latest 5-minute
    # liquidity snapshots. Only a new threshold crossing fetches live books.
    if funding_targets and registry is not None:
        fx_cache = UsdcUsdtQuoteCache(scrapers.get("binance_spot"))
        sched.add_job(
            check_dislocations,
            trigger=IntervalTrigger(seconds=300),
            args=[
                storage,
                notifier,
                state,
                registry,
                settings.spread_alert_apr,
                scrapers,
                fx_cache,
            ],
            id="alert:dislocations", name="alert:dislocations",
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
    registry = SymbolRegistry.load(settings.symbols_file)
    sched = build_scheduler(scrapers, storage, notifier, state,
                            ohlcv_targets, fills_targets, funding_targets, liquidity_targets,
                            registry=registry)
    sched.start()
    log.info("scheduler started with %d job(s)", len(sched.get_jobs()))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # -- websocket streams (plain asyncio tasks, not APScheduler jobs) ---------
    # A stream is a long-lived connection, not something to trigger on an
    # interval, so it lives beside the scheduler rather than inside it. Both
    # share `stop`, so one SIGTERM winds down everything.
    stream_targets = load_stream_targets(settings.symbols_file)
    stream_tasks: list[asyncio.Task] = []
    capture: SettlementCapture | None = None

    if any(t.capture for t in stream_targets):
        capture = SettlementCapture()
        # Seed before any stream starts, so a settlement in the first few
        # minutes is not missed while waiting on the first refresh tick.
        capture.refresh(await storage.latest_funding_rows())
        log.info("settlement capture: %d symbol(s) scheduled, window -%.0fs/+%.0fs",
                 capture.tracked, capture.pre_seconds, capture.post_seconds)
        stream_tasks.append(
            asyncio.create_task(capture.run(storage, stop), name="capture:schedule")
        )

    for target in stream_targets:
        stream = STREAM_REGISTRY[target.venue](
            symbols=target.symbols, venue=target.venue
        )
        mode = "settlement-capture" if target.capture else "continuous tape"
        log.info("stream %s: %d symbol(s), mode=%s",
                 stream.stream_id, len(target.symbols), mode)
        stream_tasks.append(
            asyncio.create_task(
                run_stream(stream, storage, notifier, state, stop,
                           gate=capture if target.capture else None),
                name=stream.stream_id,
            )
        )

    try:
        await stop.wait()
    finally:
        log.info("shutting down…")
        sched.shutdown(wait=False)
        # Streams flush their buffers on stop, so give them a moment to land
        # rather than cancelling straight into a closing database pool.
        if stream_tasks:
            done, pending = await asyncio.wait(stream_tasks, timeout=10)
            for task in pending:
                log.warning("stream task %s did not stop in time; cancelling",
                            task.get_name())
                task.cancel()
            await asyncio.gather(*stream_tasks, return_exceptions=True)
        for s in scrapers.values():
            await s.aclose()
        await notifier.aclose()
        await storage.close()


def cli() -> None:
    """Console-script entrypoint — sync wrapper around the async main()."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()