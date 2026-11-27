from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from data_collection.http import HttpClient

log = logging.getLogger("overseer.scheduler")

_DISCORD_LIMIT = 2000         
_TRUNCATE = 1900               

# Failures and recoveries wait this long after the first for their venue, so a
# venue going down or coming back is one post rather than one per job: every
# minute job reports within a cycle, its jitter and a timed-out request.
GROUP_SECONDS = 90.0

# Job ids name their venue second (ohlcv:lighter:BTC:1m, stream:binance_perp);
# other jobs group by kind (execution:BTC as execution, tvl:lighter as tvl).
_VENUE_JOBS = frozenset({"ohlcv", "orderbook", "funding", "liquidity", "fills", "stream"})

# A post names up to this many of a group's jobs; more are counted by kind.
_NAMED_JOBS = 3


class Channel(StrEnum):
    """A kind of post. Each may have its own webhook; without one it goes to
    the default webhook, as does anything not in a channel (TVL drops)."""

    ERRORS = "errors"        # job failures and recoveries, daily heartbeat, funding units
    FUNDING = "funding"      # current-funding dislocations
    LISTINGS = "listings"    # the daily listings check


_TRADE_URLS: dict[str, Callable[[str], str]] = {
    "binance":     lambda s: f"https://www.binance.com/en/futures/{s.replace('/', '')}",
    "bybit":       lambda s: f"https://www.bybit.com/trade/usdt/{s.replace('/', '')}",
    "hyperliquid": lambda s: f"https://app.hyperliquid.xyz/trade/{s}",
    "lighter":     lambda s: f"https://app.lighter.xyz/trade/{s}",
    "extended":    lambda s: f"https://app.extended.exchange/trade/{s}",
    "rise":        lambda s: f"https://www.rise.trade/en/trade/{s.split('/')[0]}",
    "bullet":      lambda s: f"https://app.bullet.xyz/trade/{s}",
    "qfex":        lambda s: f"https://www.qfex.com/trade/{s}",
    "phoenix":     lambda s: f"https://www.phoenix.trade/markets/{s}",
}


def trade_url(exchange: str, symbol: str) -> str | None:
    """Deep-link to a venue's trading UI for one symbol, or None if the venue
    isn't in _TRADE_URLS yet."""
    fn = _TRADE_URLS.get(exchange)
    return fn(symbol) if fn else None


def trade_link(exchange: str, symbol: str) -> str:
    """Discord-markdown link for an exchange name, or plain bold text if no
    trade URL is known for it yet — always safe to embed in a message."""
    url = trade_url(exchange, symbol)
    return f"[{exchange}]({url})" if url else f"**{exchange}**"


def alert_group(job_id: str) -> str:
    kind, _, rest = job_id.partition(":")
    return rest.partition(":")[0] if kind in _VENUE_JOBS and rest else kind


@dataclass(frozen=True)
class _Change:
    status: str                  # 'fail' | 'ok'
    error: str | None = None
    fetched: int = 0
    new_rows: int = 0


@dataclass
class _Held:
    since: float                 # monotonic time of the group's first change
    changes: dict[str, _Change] = field(default_factory=dict)   # job_id -> latest


def _summary(changes: dict[str, _Change], verb: str) -> str:
    """'2 jobs failing — a, b', or past _NAMED_JOBS counts by kind:
    '42 jobs failing — ohlcv 21, orderbook 21'."""
    if len(changes) <= _NAMED_JOBS:
        names = ", ".join(changes)
    else:
        kinds = Counter(job.partition(":")[0] for job in changes)
        names = ", ".join(f"{kind} {n}" for kind, n in sorted(kinds.items()))
    return f"{len(changes)} job{'' if len(changes) == 1 else 's'} {verb} — {names}"


def _errors(failing: dict[str, _Change]) -> str:
    counts = Counter(
        (change.error or "unknown error").partition("\n")[0][:200]
        for change in failing.values()
    )
    lines = [f"×{n} {error}" for error, n in counts.most_common(3)]
    if len(counts) > 3:
        lines.append(f"… and {len(counts) - 3} other errors")
    return "```\n" + "\n".join(lines) + "\n```"


def _pack(blocks: list[str]) -> list[str]:
    """Blocks joined into as few posts as Discord's length limit allows."""
    posts: list[str] = []
    for block in blocks:
        if posts and len(posts[-1]) + 2 + len(block) <= _DISCORD_LIMIT:
            posts[-1] += "\n\n" + block
        else:
            posts.append(block)
    return posts


class DiscordNotifier:
    """Posts to Discord webhooks. Job failures and recoveries are held per
    venue for `group_seconds` and posted together by run(); digests post at once."""

    def __init__(
        self, webhook_url: str | None, http: HttpClient | None = None,
        *, channels: Mapping[Channel, str | None] | None = None,
        group_seconds: float = GROUP_SECONDS,
    ) -> None:
        self._url = webhook_url
        self._urls = {channel: url for channel, url in (channels or {}).items() if url}
        self._http = http or HttpClient(limiter=None, max_retries=2)
        self._group_seconds = group_seconds
        self._held: dict[str, _Held] = {}       # alert group -> changes to post
        self._posted: dict[str, str] = {}       # job_id -> status last posted

    @property
    def enabled(self) -> bool:
        return bool(self._url or self._urls)

    def webhook_for(self, channel: Channel) -> str:
        """'own', 'default' or 'off': where the channel's posts go."""
        return "own" if channel in self._urls else "default" if self._url else "off"

    async def _send(self, content: str, channel: Channel | None = None) -> None:
        url = self._urls.get(channel) if channel else None
        url = url or self._url
        if not url:
            return
        if len(content) > _DISCORD_LIMIT:
            content = content[:_TRUNCATE] + "\n… (truncated)"
        try:
            await self._http.request(
                "post", url, json={"content": content, "flags": 4}
            )
        except Exception:

            log.warning("discord notification failed to send", exc_info=True)

    def failure(self, job_id: str, error: str | None) -> None:
        self._hold(job_id, _Change("fail", error))

    def recovery(self, job_id: str, fetched: int, new_rows: int) -> None:
        self._hold(job_id, _Change("ok", None, fetched, new_rows))

    def _hold(self, job_id: str, change: _Change) -> None:
        group = alert_group(job_id)
        held = self._held.get(group)
        if held is None:
            held = self._held[group] = _Held(time.monotonic())
        held.changes[job_id] = change

    def _describe(self, group: str, changes: dict[str, _Change]) -> str | None:
        """A group's post, or None when every job is back where its last post
        left it, such as a failure that recovered within the window."""
        changed = {
            job: change for job, change in sorted(changes.items())
            if change.status != self._posted.get(job, "ok")
        }
        for job, change in changed.items():
            self._posted[job] = change.status
        if len(changed) == 1:
            (job, change), = changed.items()
            if change.status == "fail":
                err = (change.error or "unknown error")[:1500]
                return f"🔴 **{job}** entered a failing state\n```{err}```"
            return f"🟢 **{job}** recovered (fetched {change.fetched}, {change.new_rows} new)"
        failing = {job: c for job, c in changed.items() if c.status == "fail"}
        recovered = {job: c for job, c in changed.items() if c.status == "ok"}
        lines = []
        if failing:
            lines += [f"🔴 **{group}**: {_summary(failing, 'failing')}", _errors(failing)]
        if recovered:
            lines.append(f"🟢 **{group}**: {_summary(recovered, 'recovered')}")
        return "\n".join(lines) or None

    async def flush(self, *, everything: bool = False) -> None:
        """Post the groups held for group_seconds, or every group."""
        cutoff = time.monotonic() - self._group_seconds
        due = sorted(g for g, held in self._held.items() if everything or held.since <= cutoff)
        blocks = [self._describe(group, self._held.pop(group).changes) for group in due]
        for post in _pack([block for block in blocks if block]):
            await self._send(post, Channel.ERRORS)

    async def run(self, stop: asyncio.Event, tick: float = 5.0) -> None:
        """Post groups as they come due until `stop`, then everything held."""
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=tick)
            await self.flush(everything=stop.is_set())
            if stop.is_set():
                return

    async def digest(self, text: str, channel: Channel | None = None) -> None:
        await self._send(text, channel)

    async def aclose(self) -> None:
        await self._http.aclose()