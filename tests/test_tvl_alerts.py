from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from core.enums import Exchange
from core.models import VenueTvl
from scheduler.jobs import run_tvl


class FakeDefiLlama:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    async def fetch(self, slug: str) -> Decimal | None:
        value = self.values[slug]
        if isinstance(value, Exception):
            raise value
        return value


class TvlStorage:
    def __init__(self, records: list[VenueTvl] | None = None) -> None:
        self.records = list(records or [])

    async def write_venue_tvl(self, records: list[VenueTvl]) -> int:
        self.records.extend(records)
        return len(records)

    async def venue_tvl_since(self, since: datetime) -> list[dict[str, Any]]:
        rows = [r for r in self.records if r.ts >= since]
        return [{"exchange": r.exchange.value, "ts": r.ts, "tvl_usd": r.tvl_usd}
                for r in sorted(rows, key=lambda r: (r.exchange.value, r.ts))]

    async def record_job_run(self, *args: Any) -> None:
        pass


class Notifier:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.failures: list[str] = []

    async def digest(self, text: str) -> None:
        self.messages.append(text)

    async def failure(self, job_id: str, error: str | None) -> None:
        self.failures.append(job_id)

    async def recovery(self, job_id: str, fetched: int, new_rows: int) -> None:
        self.messages.append(f"recovered {job_id}")


@pytest.mark.asyncio
async def test_tvl_fall_from_the_trailing_high_alerts_once_until_steady() -> None:
    now = datetime.now(timezone.utc)
    storage = TvlStorage([
        VenueTvl(Exchange.LIGHTER, now - timedelta(hours=5), Decimal("580000000")),
        VenueTvl(Exchange.LIGHTER, now - timedelta(hours=3), Decimal("590000000")),
        # Older than the six-hour window, so never the high.
        VenueTvl(Exchange.LIGHTER, now - timedelta(hours=7), Decimal("900000000")),
    ])
    llama = FakeDefiLlama({"lighter-bridge": Decimal("550000000")})
    notifier, state = Notifier(), {}

    async def poll(tvl: str) -> None:
        llama.values["lighter-bridge"] = Decimal(tvl)
        await run_tvl(llama, [Exchange.LIGHTER], storage, notifier, state, 10, 6)  # type: ignore[arg-type]

    await poll("550000000")                 # 6.8% below the $590M high
    assert notifier.messages == []

    await poll("520000000")                 # 11.9% below it
    assert len(notifier.messages) == 1
    alert = notifier.messages[0]
    assert "**Lighter** TVL down 11.9% from its 6h high: $590M" in alert
    assert "→ $520M" in alert and "<https://defillama.com/protocol/lighter-bridge>" in alert

    await poll("500000000")                 # still falling: no repeat
    assert len(notifier.messages) == 1

    await poll("585000000")                 # within half the threshold of the high
    assert notifier.messages[1] == "↩️ **Lighter** TVL steady again: 0.8% below its 6h high"


@pytest.mark.asyncio
async def test_tvl_gaps_are_not_stored_and_alert_per_venue_unless_defillama_is_down() -> None:
    venues = [Exchange.HYPERLIQUID, Exchange.LIGHTER, Exchange.BULK]
    storage = TvlStorage()
    notifier, state = Notifier(), {}
    llama = FakeDefiLlama({
        "hyperliquid-bridge": Decimal("6300000000"),
        "lighter-bridge": None,             # DefiLlama answered with nothing
        "bulk": Decimal("0"),               # or with zero: an outage, not a drain
    })

    await run_tvl(llama, venues, storage, notifier, state, 10, 6)  # type: ignore[arg-type]

    assert [(r.exchange, r.tvl_usd) for r in storage.records] == [
        (Exchange.HYPERLIQUID, Decimal("6300000000")),
    ]
    assert notifier.failures == ["tvl:lighter", "tvl:bulk"]
    assert notifier.messages == []

    # When every venue fails at once, only the job itself alerts.
    llama.values = {slug: RuntimeError("DefiLlama down") for slug in llama.values}
    await run_tvl(llama, venues, storage, notifier, state, 10, 6)  # type: ignore[arg-type]
    assert notifier.failures == ["tvl:lighter", "tvl:bulk", "tvl:defillama"]
