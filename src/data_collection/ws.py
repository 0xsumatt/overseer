"""Supervised websocket transport for streaming venue feeds.

The streaming counterpart to http.py, and the same bargain: this module is the
single place that knows about picows, so swapping the websocket library changes
this file and nothing else. Venue streams never touch a socket — they declare
what to subscribe to and how to parse a payload (see streams.py).

What it folds in, so no venue re-implements it:

  * **reconnect supervision** — exponential backoff with full jitter, the same
    posture HttpClient uses for retries, running until explicitly stopped;
  * **subscription replay** — a reconnect is invisible to the venue adapter;
    its subscribe frames are re-sent on every fresh connection;
  * **liveness accounting** — separately tracking "the socket is alive" and
    "the subscription is delivering", which are NOT the same thing (see below).

Two liveness signals, deliberately distinct
-------------------------------------------
`last_frame_at` advances on ANY frame including pongs; `last_payload_at` only
on data frames. A feed that is connected, answering pings, and silently
delivering nothing — a dropped subscription, a venue that quietly stopped
publishing — looks perfectly healthy on the first signal and dead on the
second. That combination is the whole reason a poll-shaped health model
doesn't work for streams, so both are exposed and the supervisor alerts on the
second (see scheduler/streams.py).

picows specifics worth knowing before editing
---------------------------------------------
* `WSListener` callbacks are **synchronous** — that is where picows' speed
  comes from, and it is a hard constraint on everything downstream: no awaits,
  no database writes, no slow parsing inside `on_ws_frame`. The callback
  parses and drops records into an in-memory buffer; an async task drains it
  (storage/buffer.py). Blocking here stalls the read loop and drops frames.
* Ping/pong is the library's job, not ours: `enable_auto_ping` sends pings when
  idle and drops the connection if a reply never arrives, which surfaces to us
  as a normal disconnect and therefore a normal reconnect.
* `frame.get_payload_as_bytes()` returns a copy, so it is safe to hand onward.
  `get_payload_as_memoryview()` is the zero-copy variant and is NOT safe to
  retain past the callback — do not "optimise" into it without also making the
  parse fully eager.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from picows import WSFrame, WSListener, WSMsgType, WSTransport, ws_connect

log = logging.getLogger("overseer.ws")


@dataclass(frozen=True)
class WsHealth:
    """Point-in-time view of a connection, for the heartbeat translator."""

    connected: bool
    frames: int                     # data frames since the last read
    reconnects: int
    seconds_since_frame: float | None    # any frame — socket alive?
    seconds_since_payload: float | None  # data frame — subscription alive?
    last_error: str | None


class _Listener(WSListener):
    """Bridges picows' sync callbacks onto the owning client's counters."""

    def __init__(self, client: "WsClient") -> None:
        self._client = client

    def on_ws_connected(self, transport: WSTransport) -> None:
        self._client._on_connected(transport)

    def on_ws_frame(self, transport: WSTransport, frame: WSFrame) -> None:
        client = self._client
        now = time.monotonic()
        client._last_frame_at = now

        msg_type = frame.msg_type
        if msg_type == WSMsgType.CLOSE:
            # Answer the close handshake and let the supervisor reconnect.
            with contextlib.suppress(Exception):
                transport.send_close(frame.get_close_code())
                transport.disconnect()
            return
        if msg_type not in (WSMsgType.TEXT, WSMsgType.BINARY):
            return                   # ping/pong/continuation: liveness only

        client._frames += 1
        client._last_payload_at = now
        try:
            client._on_payload(frame.get_payload_as_bytes())
        except Exception:
            # One malformed message must never kill the read loop; the venue
            # would otherwise reconnect in a hot loop on a single bad frame.
            client._parse_errors += 1
            log.exception("%s: payload handler raised", client.name)

    def on_ws_disconnected(self, transport: WSTransport) -> None:
        self._client._on_disconnected()


class WsClient:
    """One supervised connection. Reconnects until stopped.

    `subscribe_frames` is called on every (re)connection rather than once, so
    the venue adapter can build frames fresh — some venues want a nonce or a
    timestamp in the subscribe payload.
    """

    def __init__(
        self,
        name: str,
        url: str,
        *,
        on_payload: Callable[[bytes], None],
        subscribe_frames: Callable[[], Sequence[bytes]] | None = None,
        keepalive_frames: Callable[[], Sequence[bytes]] | None = None,
        keepalive_seconds: float | None = None,
        headers: dict[str, str] | None = None,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        ping_idle_timeout: float = 15.0,
        ping_reply_timeout: float = 10.0,
    ) -> None:
        self.name = name
        self._url = url
        self._on_payload = on_payload
        self._subscribe_frames = subscribe_frames or (lambda: ())
        self._keepalive_frames = keepalive_frames
        self._keepalive_seconds = keepalive_seconds
        self._headers = headers or {}
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._ping_idle = ping_idle_timeout
        self._ping_reply = ping_reply_timeout

        self._connected = False
        self._frames = 0
        self._parse_errors = 0
        self._reconnects = 0
        self._last_frame_at: float | None = None
        self._last_payload_at: float | None = None
        self._last_error: str | None = None
        self._transport: WSTransport | None = None

    # -- counters ----------------------------------------------------------------

    def _on_connected(self, transport: WSTransport) -> None:
        self._connected = True
        self._transport = transport
        self._last_frame_at = time.monotonic()
        log.info("%s: connected", self.name)

    def _on_disconnected(self) -> None:
        self._connected = False
        self._transport = None
        log.warning("%s: disconnected", self.name)

    def health(self, *, reset_frames: bool = False) -> WsHealth:
        """Snapshot the counters. `reset_frames` zeroes the frame count so the
        caller reads frames-since-last-heartbeat rather than a lifetime total."""
        now = time.monotonic()
        snap = WsHealth(
            connected=self._connected,
            frames=self._frames,
            reconnects=self._reconnects,
            seconds_since_frame=(
                None if self._last_frame_at is None else now - self._last_frame_at
            ),
            seconds_since_payload=(
                None if self._last_payload_at is None else now - self._last_payload_at
            ),
            last_error=self._last_error,
        )
        if reset_frames:
            self._frames = 0
        return snap

    def _backoff(self, attempt: int) -> float:
        ceiling = min(self._backoff_max, self._backoff_base * (2 ** attempt))
        return random.uniform(0.0, ceiling)

    # -- the supervisor loop -------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Connect, subscribe, pump until the socket drops, then reconnect.

        Returns only once `stop` is set. Never raises for connection problems:
        an unreachable venue is an alerting condition (via health()), not a
        reason to tear down the process.
        """
        attempt = 0
        while not stop.is_set():
            try:
                transport, _ = await ws_connect(
                    lambda: _Listener(self),
                    self._url,
                    enable_auto_ping=True,
                    auto_ping_idle_timeout=self._ping_idle,
                    auto_ping_reply_timeout=self._ping_reply,
                    extra_headers=self._headers or None,
                )
            except Exception as exc:
                self._last_error = repr(exc)
                delay = self._backoff(attempt)
                log.warning("%s: connect failed (%r), retrying in %.1fs",
                            self.name, exc, delay)
                attempt += 1
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                continue

            attempt = 0
            self._last_error = None
            try:
                for payload in self._subscribe_frames():
                    transport.send(WSMsgType.TEXT, payload)
            except Exception as exc:
                # A bad subscribe is a permanent, venue-side problem; record it
                # and let the loop reconnect rather than spinning silently.
                self._last_error = f"subscribe failed: {exc!r}"
                log.exception("%s: subscribe failed", self.name)

            keepalive = None
            if self._keepalive_frames is not None and self._keepalive_seconds:
                keepalive = asyncio.ensure_future(
                    self._keepalive(transport, stop)
                )
            try:
                await self._pump(transport, stop)
            finally:
                if keepalive is not None:
                    keepalive.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await keepalive

            if not stop.is_set():
                self._reconnects += 1
                delay = self._backoff(0)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)

        with contextlib.suppress(Exception):
            if self._transport is not None:
                self._transport.disconnect()

    async def _keepalive(self, transport: WSTransport, stop: asyncio.Event) -> None:
        """Send the venue's application-level keepalive until disconnect.

        Distinct from picows' auto-ping, which operates at the websocket
        protocol level: some venues ignore protocol pings for idle-timeout
        purposes and want their own JSON heartbeat instead.
        """
        assert self._keepalive_frames is not None and self._keepalive_seconds
        while not stop.is_set() and not transport.is_disconnected:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._keepalive_seconds)
            if stop.is_set() or transport.is_disconnected:
                return
            try:
                for payload in self._keepalive_frames():
                    transport.send(WSMsgType.TEXT, payload)
            except Exception:
                # A failed keepalive means the socket is going down anyway;
                # let the disconnect path handle it rather than raising here.
                log.warning("%s: keepalive send failed", self.name, exc_info=True)
                return

    async def _pump(self, transport: WSTransport, stop: asyncio.Event) -> None:
        """Wait for either the socket to drop or a shutdown request.

        Frames arrive on picows' own callbacks, so there is nothing to read
        here — this just parks until one of the two exits happens.
        """
        disconnected = asyncio.ensure_future(transport.wait_disconnected())
        stopping = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait(
                {disconnected, stopping}, return_when=asyncio.FIRST_COMPLETED
            )
            if stopping.done():
                with contextlib.suppress(Exception):
                    transport.disconnect()
                with contextlib.suppress(Exception):
                    await disconnected
        finally:
            for task in (disconnected, stopping):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
