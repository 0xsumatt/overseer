from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, ClassVar

from core.enums import Exchange, MarketType, Timeframe
from core.models import OHLCV, LeverageStats
from data_collection.base import BaseExchangeScraper, Capability
from data_collection.http import HttpClient, HttpError
from data_collection.ratelimit import RateLimiter

_CHALLENGE_MARKERS = ("Vercel Security Checkpoint", "<!DOCTYPE html")


class HyloChallenged(RuntimeError):
    """The endpoint served the anti-bot interstitial instead of JSON.

    Its own error type so a blocked poll is unmistakable in job_runs and in the
    Discord alert — the alternative is a JSON-decode traceback, or (because the
    checkpoint answers with 429) a bogus "rate limited" that would send someone
    tuning the limiter for no reason.
    """


def _is_challenge(body: str | None) -> bool:
    return bool(body) and any(m in body[:2000] for m in _CHALLENGE_MARKERS)


class HyloScraper(BaseExchangeScraper):
    exchange: ClassVar[Exchange] = Exchange.HYLO
    base_url: ClassVar[str] = "https://hylo.so"
    market_type: ClassVar[MarketType] = MarketType.LEVERAGED_TOKEN
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.OHLCV, Capability.LEVERAGE_STATS}
    )

    # How many `before` pages one fetch will walk. Page size is unknown, so this
    # is a backstop against an endpoint that never stops paging, not a tuned
    # number — a resuming poll only ever needs one page.
    MAX_PAGES: ClassVar[int] = 20

    def _build_http(self) -> HttpClient:
        return HttpClient(
            # Undocumented internal API on someone else's infrastructure: poll
            # gently. max_retries=1 because the failure we actually expect is
            # the challenge, and retrying that just burns time — genuine
            # transient errors still get one more go.
            limiter=RateLimiter.per_minute(30, burst=5),
            default_headers={"User-Agent": "overseer/0.1"},
            max_retries=1,
        )

    # -- symbols: token tickers, identity ----------------------------------------

    def to_symbol(self, native: str) -> str:
        return native

    def to_native(self, symbol: str) -> str:
        return symbol

    @classmethod
    def market_type_for(cls, symbol: str) -> MarketType:
        return MarketType.LEVERAGED_TOKEN

    # -- unix SECONDS (base class helpers are milliseconds) -----------------------

    @staticmethod
    def _to_s(dt: datetime) -> int:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())

    @staticmethod
    def _from_s(s: int | float) -> datetime:
        return datetime.fromtimestamp(s, tz=timezone.utc)

    # -- one chart call -----------------------------------------------------------

    async def _chart(
        self, kind: str, symbol: str, interval: Timeframe, before: int
    ) -> list[dict]:
        """One page of a chart series, newest-first, ending at `before`.

        Parses the body by hand rather than via get_json so the challenge page
        is caught whether it arrives as an error status or a 200 with HTML.
        """
        url = f"{self.base_url}/api/chart"
        params = {
            "type": kind,
            "timeframe": interval.value,
            "asset": self.to_native(symbol),
            "before": str(before),
        }
        try:
            resp = await self.http.request("get", url, params=params)
            text = await resp.text()
        except HttpError as exc:
            if _is_challenge(exc.body):
                raise HyloChallenged(
                    f"hylo {kind}/{symbol}: served the Vercel challenge page "
                    f"(HTTP {exc.status}) — no JSON available from a plain client"
                ) from exc
            raise

        if _is_challenge(text):
            raise HyloChallenged(
                f"hylo {kind}/{symbol}: served the Vercel challenge page (HTTP 200)"
            )

        payload: Any = json.loads(text)
        if not payload.get("success", True):
            raise RuntimeError(f"hylo {kind}/{symbol}: success=false — {text[:200]}")
        data = payload.get("data") or []
        return [r for r in data if isinstance(r, dict)]

    async def _chart_paged(
        self, kind: str, symbol: str, interval: Timeframe, since: datetime
    ) -> dict[int, dict]:
        """Walk `before` backwards until the series covers `since`.

        Returns {unix_seconds: row}. Stops on an empty page, on reaching
        `since`, or if a page fails to move the window back — that last guard
        matters because an endpoint that ignores `before` would otherwise
        return the same newest page forever.
        """
        since_s = self._to_s(since)
        before = self._to_s(datetime.now(timezone.utc))
        rows: dict[int, dict] = {}

        for _ in range(self.MAX_PAGES):
            page = await self._chart(kind, symbol, interval, before)
            times = [int(r["time"]) for r in page if r.get("time") is not None]
            if not times:
                break
            for r in page:
                t = r.get("time")
                if t is not None:
                    rows[int(t)] = r
            earliest = min(times)
            if earliest <= since_s or earliest >= before:
                break
            before = earliest - 1        # -1 so the boundary bar isn't re-fetched

        return {t: r for t, r in rows.items() if t >= since_s}

    @staticmethod
    def _value_of(row: dict) -> Any:
        """Scalar out of a chart row, whichever shape the endpoint uses.

        exo-price is confirmed OHLC; the total-value and leverage-ratio shapes
        are not, so accept a plain `value`/`close` scalar or an OHLC bar's
        close. Returns None if neither is present, which the caller treats as
        "no reading at this timestamp" rather than zero.
        """
        for field in ("value", "close", "v"):
            if row.get(field) is not None:
                return row[field]
        return None

    # -- OHLCV: token NAV ---------------------------------------------------------

    async def fetch_ohlcv(
        self, symbol: str, interval: Timeframe, since: datetime
    ) -> Sequence[OHLCV]:
        rows = await self._chart_paged("exo-price", symbol, interval, since)
        out = [
            OHLCV(
                exchange=self.exchange,
                market_type=MarketType.LEVERAGED_TOKEN,
                symbol=self.to_symbol(symbol),
                interval=interval,
                ts=self._from_s(t),
                open=self._dec(r["open"]),
                high=self._dec(r["high"]),
                low=self._dec(r["low"]),
                close=self._dec(r["close"]),
                # No volume on a mint/redeem product, and the column is NOT NULL.
                # Zero is the honest reading: there is no secondary tape here,
                # and any chart drawing volume for hylo would be drawing a lie.
                volume=Decimal(0),
            )
            for t, r in rows.items()
        ]
        out.sort(key=lambda b: b.ts)
        return out

    # -- leverage stats: two series, merged on bar timestamp ----------------------

    async def fetch_leverage_stats(
        self, symbol: str, interval: Timeframe, since: datetime
    ) -> Sequence[LeverageStats]:
        """Pool TVL + effective leverage.

        Two endpoints, merged on timestamp. Whether they actually share bar
        timestamps is exactly what checklist item 4 in the module docstring is
        for; until that is observed, the union is taken and each side may be
        None, which is why migration 006 leaves both columns nullable.
        """
        tv = await self._chart_paged("exo-total-value", symbol, interval, since)
        lev = await self._chart_paged("exo-leverage-ratio", symbol, interval, since)

        out: list[LeverageStats] = []
        for t in sorted(set(tv) | set(lev)):
            total_value = self._value_of(tv.get(t) or {})
            leverage = self._value_of(lev.get(t) or {})
            if total_value is None and leverage is None:
                continue
            out.append(
                LeverageStats(
                    exchange=self.exchange,
                    symbol=self.to_symbol(symbol),
                    interval=interval,
                    ts=self._from_s(t),
                    total_value=self._dec(total_value) if total_value is not None else None,
                    leverage=self._dec(leverage) if leverage is not None else None,
                )
            )
        return out
