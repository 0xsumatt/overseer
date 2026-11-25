from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from data_collection.exchanges.bybit import BybitPerpScraper

THROTTLED = {"retCode": 10006, "retMsg": "Too many visits. Exceeded the API Rate Limit."}


class Http:
    def __init__(self, *payloads: dict[str, Any]) -> None:
        self.payloads = list(payloads)
        self.pauses: list[float] = []

    async def get_json(self, url: str, **_: Any) -> Any:
        return self.payloads.pop(0)

    def pause(self, seconds: float) -> None:
        self.pauses.append(seconds)


@pytest.mark.asyncio
async def test_a_throttled_request_waits_and_retries_rather_than_failing_the_run() -> None:
    history = {"retCode": 0, "result": {"list": [
        {"symbol": "SOLUSDT", "fundingRateTimestamp": "1700000000000", "fundingRate": "0.0001"},
    ]}}
    instrument = {"retCode": 0, "result": {"list": [{"fundingInterval": 480}]}}
    http = Http(THROTTLED, history, instrument)
    [row] = await BybitPerpScraper(http).fetch_funding(  # type: ignore[arg-type]
        "SOL/USDT", datetime(2023, 11, 1, tzinfo=timezone.utc)
    )
    assert (row.rate, row.interval_hours) == (Decimal("0.0001"), 8)
    # Every Bybit request is held back a second, not stalled for 30 s.
    assert http.pauses == [1.0]

    http = Http(THROTTLED, THROTTLED, THROTTLED)
    with pytest.raises(RuntimeError, match=r"10006: .* \(still throttled after 2 retries\)"):
        await BybitPerpScraper(http).fetch_bbo("SOL/USDT")  # type: ignore[arg-type]
    assert http.pauses == [1.0, 2.0]

    # Any other error fails at once.
    http = Http({"retCode": 10001, "retMsg": "params error"})
    with pytest.raises(RuntimeError, match="bybit error 10001: params error$"):
        await BybitPerpScraper(http).fetch_bbo("SOL/USDT")  # type: ignore[arg-type]
    assert http.pauses == []
