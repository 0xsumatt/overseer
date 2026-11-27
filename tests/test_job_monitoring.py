from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from scheduler.notify import Channel, DiscordNotifier, alert_group
from storage.writers import Storage

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Webhook:
    def __init__(self) -> None:
        self.posts: list[str] = []
        self.urls: list[str] = []

    async def request(self, method: str, url: str, *, json: dict[str, Any]) -> None:
        self.posts.append(json["content"])
        self.urls.append(url)

    async def aclose(self) -> None:
        pass


def _notifier(group_seconds: float) -> tuple[DiscordNotifier, Webhook]:
    webhook = Webhook()
    notifier = DiscordNotifier("https://discord.test/hook", webhook,  # type: ignore[arg-type]
                               group_seconds=group_seconds)
    return notifier, webhook


@pytest.mark.asyncio
async def test_each_kind_of_post_goes_to_its_own_webhook_or_else_the_default() -> None:
    webhook = Webhook()
    notifier = DiscordNotifier("https://default", webhook, group_seconds=0,  # type: ignore[arg-type]
                               channels={Channel.ERRORS: "https://errors",
                                         Channel.FUNDING: "https://funding",
                                         Channel.LISTINGS: None})
    notifier.failure("ohlcv:bulk:BTC:1m", "boom")
    await notifier.flush()
    await notifier.digest("spread", Channel.FUNDING)
    await notifier.digest("listings", Channel.LISTINGS)
    await notifier.digest("tvl")
    assert webhook.urls == ["https://errors", "https://funding", "https://default", "https://default"]

    # Without a default, only channels with their own webhook post.
    webhook = Webhook()
    notifier = DiscordNotifier(None, webhook, channels={Channel.LISTINGS: "https://listings"})  # type: ignore[arg-type]
    await notifier.digest("spread", Channel.FUNDING)
    await notifier.digest("listings", Channel.LISTINGS)
    assert (webhook.urls, notifier.enabled) == (["https://listings"], True)


def test_jobs_group_by_their_venue_or_else_their_kind() -> None:
    assert {job: alert_group(job) for job in (
        "ohlcv:hyperliquid_xyz:xyz:GOLD:1m", "stream:binance_perp:trades",
        "liquidity:lighter", "execution:BTC", "tvl:lighter", "venue_volume:daily",
    )} == {
        "ohlcv:hyperliquid_xyz:xyz:GOLD:1m": "hyperliquid_xyz",
        "stream:binance_perp:trades": "binance_perp",
        "liquidity:lighter": "lighter",
        "execution:BTC": "execution",
        "tvl:lighter": "tvl",
        "venue_volume:daily": "venue_volume",
    }


@pytest.mark.asyncio
async def test_a_venue_outage_is_held_then_posted_once_with_counts_and_errors() -> None:
    notifier, webhook = _notifier(group_seconds=90)
    for symbol in ("BTC", "ETH", "SOL", "XRP"):
        notifier.failure(f"ohlcv:lighter:{symbol}:1m", "HttpError('HTTP 503: server error')")
        notifier.failure(f"orderbook:lighter:{symbol}", "HttpError('HTTP 503: server error')")
    notifier.failure("orderbook:lighter:DOGE", "network error after 2 retries: ReadTimeout()")
    notifier.failure("funding:hyperliquid:BTC", "HttpError('HTTP 500: server error')")

    await notifier.flush()
    assert webhook.posts == []

    await notifier.flush(everything=True)
    assert webhook.posts == [
        "🔴 **funding:hyperliquid:BTC** entered a failing state\n"
        "```HttpError('HTTP 500: server error')```\n\n"
        "🔴 **lighter**: 9 jobs failing — ohlcv 4, orderbook 5\n"
        "```\n×8 HttpError('HTTP 503: server error')\n"
        "×1 network error after 2 retries: ReadTimeout()\n```"
    ]


@pytest.mark.asyncio
async def test_a_failure_that_recovers_before_its_post_is_never_posted() -> None:
    notifier, webhook = _notifier(group_seconds=0)
    job = "ohlcv:bulk:BTC:1m"
    notifier.failure(job, "boom")
    notifier.recovery(job, 10, 1)
    await notifier.flush()
    assert webhook.posts == []

    notifier.failure(job, "boom")
    await notifier.flush()
    # Recovered and failed again between posts: still failing, nothing new to say.
    notifier.recovery(job, 10, 1)
    notifier.failure(job, "boom again")
    await notifier.flush()
    notifier.recovery(job, 12, 2)
    notifier.recovery("orderbook:bulk:BTC", 1, 1)       # never posted as failing
    await notifier.flush()
    assert webhook.posts == [
        f"🔴 **{job}** entered a failing state\n```boom```",
        f"🟢 **{job}** recovered (fetched 12, 2 new)",
    ]


@pytest.mark.asyncio
async def test_heartbeats_held_while_the_database_is_down_are_written_later() -> None:
    class Pool:
        def __init__(self) -> None:
            self.down = True
            self.writes: list[tuple[Any, ...]] = []

        async def execute(self, sql: str, *args: Any) -> None:
            if self.down:
                raise OSError("database unavailable")
            self.writes.append(args)

    storage = Storage("postgresql://unused")
    storage._pool = pool = Pool()  # type: ignore[assignment]
    storage.record_job_run("ohlcv:bulk:BTC:1m", "ok", 5, 1, None, T0)
    with pytest.raises(OSError):
        await storage.flush_job_runs()
    storage.record_job_run("ohlcv:bulk:BTC:1m", "fail", 0, 0, "HTTP 503", T0 + timedelta(minutes=1))

    pool.down = False
    assert await storage.flush_job_runs() == 1
    assert await storage.flush_job_runs() == 0
    # The newer failure is written, with the success the failed flush held.
    assert pool.writes == [(
        ["ohlcv:bulk:BTC:1m"], [T0 + timedelta(minutes=1)], ["fail"], [0], [0],
        ["HTTP 503"], [T0],
    )]


@pytest.mark.asyncio
async def test_one_heartbeat_upsert_keeps_each_jobs_latest_outcome_and_last_success() -> None:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for the PostgreSQL heartbeat regression")
    schema = f"job_runs_{uuid4().hex}"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        await connection.execute(f"SET search_path TO {schema}")
        migrations = Path(__file__).resolve().parents[1] / "migrations"
        await connection.execute((migrations / "002_job_runs.sql").read_text())
        storage = Storage(dsn)
        storage._pool = connection  # type: ignore[assignment]

        storage.record_job_run("ohlcv:bulk:BTC:1m", "ok", 5, 1, None, T0)
        await storage.flush_job_runs()
        # A success then a failure between flushes; another job never succeeds.
        storage.record_job_run("ohlcv:bulk:BTC:1m", "ok", 6, 2, None, T0 + timedelta(minutes=1))
        storage.record_job_run("ohlcv:bulk:BTC:1m", "fail", 0, 0, "HTTP 503", T0 + timedelta(minutes=2))
        storage.record_job_run("funding:bulk:BTC", "fail", 0, 0, "HTTP 503", T0 + timedelta(minutes=2))
        assert await storage.flush_job_runs() == 2
        # A later failure alone keeps the last success.
        storage.record_job_run("ohlcv:bulk:BTC:1m", "fail", 0, 0, "timeout", T0 + timedelta(minutes=3))
        await storage.flush_job_runs()

        rows = await connection.fetch(
            "SELECT job_id, last_status, last_run_at, last_success_at, last_error "
            "FROM job_runs ORDER BY job_id"
        )
        assert [tuple(row) for row in rows] == [
            ("funding:bulk:BTC", "fail", T0 + timedelta(minutes=2), None, "HTTP 503"),
            ("ohlcv:bulk:BTC:1m", "fail", T0 + timedelta(minutes=3), T0 + timedelta(minutes=1), "timeout"),
        ]
    finally:
        await connection.execute("RESET search_path")
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()
