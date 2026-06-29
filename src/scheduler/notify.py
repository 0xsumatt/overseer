"""Discord notifications via webhook — best effort, never blocking a scrape.

Reserved for things you need to *react* to: a job entering a failed state, a job
recovering, and a once-a-day digest. Routine successes do NOT come here — they
go to the job_runs table and show green on the health page. Per-run success
pings would bury real failures under thousands of "ok" lines.

A webhook needs no bot or gateway: POST {"content": "..."} to the URL. Discord
replies 204 (empty body), so we use HttpClient.request (raw response), not
post_json. Any delivery failure is logged and swallowed — alerting must never
take down ingestion.
"""

from __future__ import annotations

import logging

from data_collection.http import HttpClient

log = logging.getLogger("cryptodash.scheduler")

_DISCORD_LIMIT = 2000          # hard max on message content
_TRUNCATE = 1900               # leave room for our wrapping/formatting


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
            await self._http.request("post", self._url, json={"content": content})
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