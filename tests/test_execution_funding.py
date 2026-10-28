from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.execution_funding import holding_funding

D = Decimal
NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def current(rate, *, interval=8, age_minutes=1):
    return {"rate": D(rate), "interval_hours": interval, "ts": NOW - timedelta(minutes=age_minutes)}


def settled(rates, interval, *, spacing=None):
    spacing = spacing or timedelta(hours=interval)
    return [{"rate": D(rate), "interval_hours": interval, "ts": NOW - spacing * index}
            for index, rate in enumerate(rates)]


def cost(result, venue="a"):
    value = result[venue]["cost_bps"]
    return D(value) if value is not None else None


@pytest.mark.parametrize("hold,expected", [(8, D("1")), (24, D("3"))])
def test_holds_up_to_a_day_use_the_current_rate_pro_rata_and_shorts_receive_it(hold, expected):
    long = holding_funding(["a"], {"a": current("0.0001")}, {}, hold, True, NOW)
    short = holding_funding(["a"], {"a": current("0.0001")}, {}, hold, False, NOW)
    assert cost(long) == expected and cost(short) == -expected
    assert long["a"]["basis"] == "current" and long["a"]["unavailable"] is None


def test_a_stale_or_missing_current_rate_is_unavailable_not_zero():
    result = holding_funding(["a", "b"], {"a": current("0.0001", age_minutes=16), "b": None}, {}, 24, True, NOW)
    assert cost(result, "a") is None and "16 minutes old" in result["a"]["unavailable"]
    assert cost(result, "b") is None and result["b"]["unavailable"]


def test_longer_holds_repeat_the_previous_day_whatever_the_settlement_cadence():
    # One day of funding settled hourly, every eight hours, or recorded every
    # 15 minutes under an hourly label (BULK's collector) costs the same.
    result = holding_funding(["hourly", "eight", "bulk"], {}, {
        "hourly": settled(["0.00001"] * 24, 1),
        "eight": settled(["0.00008"] * 3, 8),
        "bulk": settled(["0.00001"] * 96, 1, spacing=timedelta(minutes=15)),
    }, 168, True, NOW)
    assert cost(result, "hourly") == cost(result, "eight") == cost(result, "bulk") == D("16.8")
    assert result["bulk"]["basis"] == "previous_24h"


def test_the_previous_day_ignores_older_settlements_and_needs_half_a_day():
    spike = {"rate": D("0.01"), "interval_hours": 1, "ts": NOW - timedelta(hours=25)}
    result = holding_funding(["a", "b"], {}, {
        "a": settled(["0.00001"] * 12, 1) + [spike],
        "b": settled(["0.00001"] * 11, 1),
    }, 720, False, NOW)
    assert cost(result, "a") == D("-72")
    assert cost(result, "b") is None and "11 of the previous 24 hours" in result["b"]["unavailable"]
