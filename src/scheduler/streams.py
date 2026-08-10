from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import Protocol

from data_collection.streams import BaseExchangeStream
from data_collection.ws import WsClient
from scheduler.jobs import JobOutcome, _record_and_alert
from scheduler.notify import DiscordNotifier
from storage.buffer import FlowBuffer, WriteBuffer
from storage.writers import Storage

log = logging.getLogger("overseer.streams")


class CaptureGate(Protocol):
    """Decides which records are worth keeping — see scheduler/capture.py.

    Two levels because a frame's symbol is only known after parsing it:
    `venue_open` is the cheap pre-parse check, `is_open` filters parsed
    records. A stream with no gate keeps everything, so the same supervisor
    serves continuous-tape and windowed-capture modes unchanged.
    """

    def venue_open(self, exchange: str) -> bool: ...
    def is_open(self, exchange: str, symbol: str) -> bool: ...


def _status(
    health, dropped_delta: int, silence_seconds: float | None
) -> tuple[str, str | None]:
    if not health.connected:
        return "fail", f"disconnected (reconnects={health.reconnects})"
    if dropped_delta > 0:
        return "fail", f"buffer shed {dropped_delta} records — writes not keeping up"
    if (
        silence_seconds is not None
        and health.seconds_since_payload is not None
        and health.seconds_since_payload > silence_seconds
    ):
        return "fail", (
            f"connected but no data for {health.seconds_since_payload:.0f}s "
            f"(threshold {silence_seconds:.0f}s) — subscription may be dead"
        )
    if health.last_error:
        return "fail", health.last_error
    return "ok", None


async def run_stream(
    stream: BaseExchangeStream,
    storage: Storage,
    notifier: DiscordNotifier,
    state: dict[str, str],
    stop: asyncio.Event,
    *,
    gate: CaptureGate | None = None,
    heartbeat_seconds: float = 30.0,
    silence_seconds: float | None = 600.0,
    max_rows: int = 1000,
    max_seconds: float = 2.0,
) -> None:
    buffer = WriteBuffer(
        storage, "write_trades", max_rows=max_rows, max_seconds=max_seconds
    )
    flow = FlowBuffer(storage)

    exchange = str(stream.exchange)

    def on_payload(payload: bytes) -> None:
        records = stream.parse(payload)
        if not records:
            return
        flow.add(records)
        if gate is None:
            buffer.add(records)
        else:
            buffer.add([r for r in records if gate.is_open(exchange, r.symbol)])

    client = WsClient(
        stream.stream_id,
        stream.ws_url,
        on_payload=on_payload,
        subscribe_frames=stream.subscribe_frames,
        headers={"User-Agent": "overseer/0.1"},
    )

    async def heartbeat() -> None:
        seen_written = 0
        seen_dropped = 0
        while not stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=heartbeat_seconds)
            if stop.is_set():
                return
            health = client.health(reset_frames=True)
            total_written = buffer.written + flow.written
            written_delta = total_written - seen_written
            dropped_delta = buffer.dropped - seen_dropped
            seen_written, seen_dropped = total_written, buffer.dropped

            status, error = _status(health, dropped_delta, silence_seconds)
            await _record_and_alert(
                JobOutcome(
                    job_id=stream.stream_id,
                    status=status,
                    fetched=health.frames,
                    new_rows=written_delta,
                    error=error,
                    ran_at=datetime.now(timezone.utc),
                ),
                storage, notifier, state,
            )

    tasks = [
        asyncio.create_task(client.run(stop), name=f"{stream.stream_id}:socket"),
        asyncio.create_task(buffer.run(stop), name=f"{stream.stream_id}:buffer"),
        asyncio.create_task(flow.run(stop), name=f"{stream.stream_id}:flow"),
        asyncio.create_task(heartbeat(), name=f"{stream.stream_id}:heartbeat"),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("%s: stopped (tape rows %d, flow rows %d, dropped %d)",
                 stream.stream_id, buffer.written, flow.written, buffer.dropped)
