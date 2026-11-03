from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal as D
from typing import Any

import pytest

from core.funding_units import resting_rates
from core.symbols import AssetMetadata
from scheduler.jobs import check_funding_units

HOURLY_FLOOR = D("0.0000125")  # 0.01% per eight hours, settled hourly: 10.95% a year
EIGHT_HOUR_FLOOR = D("0.0001")


def resting(exchange: str, rate: D, hours: int, markets: int = 3) -> list[tuple[str, D, int]]:
    return [(exchange, rate, hours)] * markets


def healthy() -> list[tuple[str, D, int]]:
    # Three venues on their own resting rates, as observed: 10.95% and 10.51%.
    return (resting("binance", EIGHT_HOUR_FLOOR, 8) + resting("hyperliquid", HOURLY_FLOOR, 1)
            + resting("lighter", D("0.000012"), 1) + [("hyperliquid", D("0.00009"), 1)])


def judged(rates) -> dict[str, Any]:
    return {rate.exchange: rate for rate in resting_rates(rates)}


@pytest.mark.parametrize("rate,hours,factor", [
    (EIGHT_HOUR_FLOOR, 1, 8),          # an 8h rate stored as hourly (Bullet, October 2026)
    (HOURLY_FLOOR / 8, 1, 1 / 8),      # divided by eight twice
    (EIGHT_HOUR_FLOOR * 100, 8, 100),  # a percentage stored as a fraction
])
def test_a_unit_error_lands_a_whole_factor_from_the_other_venues(rate, hours, factor):
    checked = judged(healthy() + resting("bullet", rate, hours))
    assert checked["bullet"].suspect
    assert checked["bullet"].factor == pytest.approx(factor, rel=0.05)
    # The other venues' median keeps one bad venue from moving the healthy ones.
    assert not any(checked[venue].suspect for venue in ("binance", "hyperliquid", "lighter"))


@pytest.mark.parametrize("markets", [
    # Genuinely high, premium-driven rates share no exact value (RISEx).
    [("rise", D(rate), 1) for rate in ("0.0000737", "0.000105", "0.00011", "0.0000138")],
    # Closed traditional markets all report zero; zero is not a resting rate.
    [("rise", D("0"), 1)] * 4,
    # Two markets sharing a rate could be chance.
    resting("rise", D("0.0001"), 1, markets=2),
])
def test_a_venue_without_a_resting_rate_is_not_judged(markets):
    assert "rise" not in judged(healthy() + markets)


def test_a_venue_needs_two_others_to_compare_against():
    assert judged(resting("binance", EIGHT_HOUR_FLOOR, 8) + resting("bullet", EIGHT_HOUR_FLOOR, 1)) == {}


class UnitStorage:
    def __init__(self) -> None:
        self.current: list[dict[str, Any]] = []
        self.settled: list[dict[str, Any]] = []

    async def latest_current_funding_rows(self, since: datetime) -> list[dict[str, Any]]:
        return self.current

    async def latest_funding_rows(self, since: datetime) -> list[dict[str, Any]]:
        return self.settled


class Notifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def digest(self, text: str) -> None:
        self.messages.append(text)


class Registry:
    """Markets named xyz:… are traditional; the rest are crypto."""

    def asset_for(self, symbol: str) -> str | None:
        return None

    def metadata(self, asset: str) -> AssetMetadata:
        family = "traditional" if asset.startswith("xyz:") else "crypto"
        return AssetMetadata(family, family, family, asset)


def rows(rates, prefix: str = "") -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    return [{"exchange": exchange, "symbol": f"{prefix}M{index}", "rate": rate,
             "interval_hours": hours, "ts": now}
            for index, (exchange, rate, hours) in enumerate(rates)]


@pytest.mark.asyncio
async def test_a_unit_error_alerts_once_and_recovery_once_per_rate_kind():
    storage, notifier, state = UnitStorage(), Notifier(), {}
    storage.settled = rows(healthy() + resting("bullet", HOURLY_FLOOR, 1))
    storage.current = rows(healthy() + resting("bullet", EIGHT_HOUR_FLOOR, 1))

    await check_funding_units(storage, notifier, state, Registry())
    await check_funding_units(storage, notifier, state, Registry())
    assert len(notifier.messages) == 1
    assert "Bullet" in notifier.messages[0] and "current" in notifier.messages[0]

    storage.current = rows(healthy() + resting("bullet", HOURLY_FLOOR, 1))
    await check_funding_units(storage, notifier, state, Registry())
    await check_funding_units(storage, notifier, state, Registry())
    assert len(notifier.messages) == 2 and "Bullet" in notifier.messages[1]
    assert state == {"units:current:bullet": "ok"}


@pytest.mark.asyncio
async def test_traditional_markets_resting_on_their_own_lower_rate_raise_nothing():
    # Lighter's equities rested at 3.50% a year beside its crypto at 10.51%.
    storage, notifier = UnitStorage(), Notifier()
    equities = rows(resting("lighter", D("0.000004"), 1, markets=6), prefix="xyz:")
    storage.current = storage.settled = rows(healthy()) + equities
    await check_funding_units(storage, notifier, {}, Registry())
    assert notifier.messages == []
