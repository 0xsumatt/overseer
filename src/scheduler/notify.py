from __future__ import annotations

import logging
from collections.abc import Callable

from data_collection.http import HttpClient

log = logging.getLogger("overseer.scheduler")

_DISCORD_LIMIT = 2000          # hard max on message content
_TRUNCATE = 1900               # leave room for our wrapping/formatting

# Deep-links to each venue's trading UI, keyed by venue-native symbol (e.g.
# "BTC/USDT" for Binance, "BTC" for Hyperliquid) — used to turn a plain
# exchange name in an alert into a clickable "go trade this" link. MUST be
# kept in sync with TRADE_URLS in web/templates/funding.html (duplicated
# rather than shared since one's Python, the other's browser JS).
#
# bulk deliberately absent: not yet enabled (see registry.py), no confirmed
# trading UI. rise/bullet/extended confirmed 2026-07-23 — note rise's URL
# wants the bare base asset ("BTC"), not the full pair ("BTC/USDC").
_TRADE_URLS: dict[str, Callable[[str], str]] = {
    "binance":     lambda s: f"https://www.binance.com/en/futures/{s.replace('/', '')}",
    "bybit":       lambda s: f"https://www.bybit.com/trade/usdt/{s.replace('/', '')}",
    "hyperliquid": lambda s: f"https://app.hyperliquid.xyz/trade/{s}",
    "lighter":     lambda s: f"https://app.lighter.xyz/trade/{s}",
    "extended":    lambda s: f"https://app.extended.exchange/trade/{s}",
    "rise":        lambda s: f"https://www.rise.trade/en/trade/{s.split('/')[0]}",
    "bullet":      lambda s: f"https://app.bullet.xyz/trade/{s}",
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


class DiscordNotifier:
    def __init__(self, webhook_url: str | None, http: HttpClient | None = None) -> None:
        self._url = webhook_url
        # no rate limiter (Discord isn't an exchange); a couple of retries so a
        # transient blip still gets through. 429/Retry-After is handled by HttpClient.
        self._http = http or HttpClient(limiter=None, max_retries=2)

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def _send(self, content: str) -> None:
        if not self._url:
            return
        if len(content) > _DISCORD_LIMIT:
            content = content[:_TRUNCATE] + "\n… (truncated)"
        try:
            # flags=4 (SUPPRESS_EMBEDS): trade links still render as clickable
            # text, but Discord won't unfurl each URL into its own preview
            # card — those cards were burying the actual alert in the channel.
            await self._http.request(
                "post", self._url, json={"content": content, "flags": 4}
            )
        except Exception:
            # alerting is best-effort; a Discord outage must not break a scrape
            log.warning("discord notification failed to send", exc_info=True)

    async def failure(self, job_id: str, error: str | None) -> None:
        err = (error or "unknown error")[:1500]
        await self._send(f"🔴 **{job_id}** entered a failing state\n```{err}```")

    async def recovery(self, job_id: str, fetched: int, new_rows: int) -> None:
        await self._send(
            f"🟢 **{job_id}** recovered (fetched {fetched}, {new_rows} new)"
        )

    async def digest(self, text: str) -> None:
        await self._send(text)

    async def aclose(self) -> None:
        await self._http.aclose()