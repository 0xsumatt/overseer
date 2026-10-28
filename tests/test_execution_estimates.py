from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.enums import Side
from core.execution import BookLevel, DepthBook, execution_estimates, round_trip_estimates

D = Decimal
NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def book(venue="a", *, bids=(("99", "2000000"),), asks=(("101", "2000000"),), quote="USD", **kwargs):
    return DepthBook(
        venue, "BTC", quote, NOW,
        tuple(BookLevel(D(price), D(size)) for price, size in bids),
        tuple(BookLevel(D(price), D(size)) for price, size in asks), **kwargs,
    )


def first(result, venue="a"):
    return result["venues"][venue]["estimates"][0]


@pytest.mark.parametrize("side,top,average,effective,fee,per_coin,impact", [
    (Side.BUY, "101", "102.2", "102.3022", "10.22", "0.1022", D("102.2") / D("101") * 10000 - 10000),
    (Side.SELL, "99", "97.8", "97.7022", "9.78", "0.0978", 10000 - D("97.8") / D("99") * 10000),
])
def test_depth_weighted_price_and_one_way_taker_cost(side, top, average, effective, fee, per_coin, impact):
    depth = book(bids=(("99", "40"), ("97", "60")), asks=(("101", "40"), ("103", "60")))
    result = execution_estimates([depth], side, {"a": D("10")}, {}, NOW)
    cell = first(result)
    assert D(result["reference_price_usd"]) == 100
    assert D(cell["quantity"]) == 100
    assert cell["status"] == "ok" and cell["complete"] and cell["best"]
    assert D(cell["top_of_book_price"]) == D(top)
    assert D(cell["top_of_book_price_usd"]) == D(top)
    assert D(cell["average_price"]) == D(average)
    assert D(cell["average_price_usd"]) == D(average)
    assert D(cell["effective_price_usd"]) == D(effective)
    assert D(cell["fee_usd"]) == D(fee)
    assert D(cell["fee_per_coin"]) == D(per_coin)
    assert D(cell["fee_per_coin_usd"]) == D(per_coin)
    assert D(cell["fee_usd"]) == D(cell["fee_per_coin_usd"]) * D(cell["quantity"])
    assert D(cell["fee_per_coin_usd"]) != D(cell["fee_usd"])
    assert abs(D(cell["price_impact_bps"]) - impact) < D("1e-20")
    assert abs(D(cell["all_in_cost_bps"]) - (impact + 10)) < D("1e-20")


@pytest.mark.parametrize("side,top,usd_price,native_fee,usd_fee", [
    (Side.BUY, "114", "102.6", "0.114", "0.1026"),
    (Side.SELL, "110", "99", "0.11", "0.099"),
])
def test_comparison_uses_one_base_quantity_and_real_fx_not_dollar_parity(side, top, usd_price, native_fee, usd_fee):
    depths = [book("usd"), book("usdc", quote="USDC", bids=(("110", "2000000"),), asks=(("114", "2000000"),))]
    result = execution_estimates(depths, side, {"usd": D("10"), "usdc": D("10")}, {"USDC": D("0.9")}, NOW)
    assert D(result["reference_price_usd"]) == D("100.4")
    assert first(result, "usd")["quantity"] == first(result, "usdc")["quantity"]
    converted = first(result, "usdc")
    assert D(converted["top_of_book_price"]) == D(top)
    assert D(converted["top_of_book_price_usd"]) == D(usd_price)
    assert D(converted["average_price"]) == D(top)
    assert D(converted["average_price_usd"]) == D(usd_price)
    assert D(converted["fee_per_coin"]) == D(native_fee)
    assert D(converted["fee_per_coin_usd"]) == D(usd_fee)
    assert D(converted["fee_per_coin_usd"]) == D(converted["fee_per_coin"]) * D("0.9")
    assert abs(D(converted["fee_usd"]) - D(usd_fee) * D(converted["quantity"])) < D("1e-24")
    assert D(converted["fee_usd"]) > D(converted["fee_per_coin_usd"])
    assert first(result, "usd")["best"]
    assert converted["best"] == (side == Side.SELL)
    assert D(first(result, "usd")["all_in_cost_bps"]) == 10
    assert D(converted["all_in_cost_bps"]) == 10


def test_visible_depth_boundary_fills_exactly_but_never_extrapolates():
    exact = book("exact", asks=(("101", "100"),))
    short = book("short", asks=(("101", "99.999"),))
    result = execution_estimates([exact, short], Side.BUY, {"exact": D("0"), "short": D("0")}, {}, NOW)
    assert first(result, "exact")["status"] == "ok"
    assert D(first(result, "exact")["top_of_book_price"]) == 101
    assert D(first(result, "exact")["top_of_book_price_usd"]) == 101
    assert D(first(result, "exact")["fee_per_coin"]) == 0
    assert D(first(result, "exact")["fee_per_coin_usd"]) == 0
    incomplete = first(result, "short")
    assert incomplete["status"] == "insufficient_depth" and not incomplete["complete"]
    assert D(incomplete["filled_quantity"]) == D("99.999")
    assert D(incomplete["fill_percent"]) == D("99.999")
    assert incomplete["average_price_usd"] is None and incomplete["effective_price_usd"] is None
    assert incomplete["all_in_cost_bps"] is None
    assert not incomplete["best"]
    assert result["venues"]["exact"]["estimates"][1]["status"] == "insufficient_depth"
    assert result["venues"]["exact"]["estimates"][1]["all_in_cost_bps"] is None
    for cell in (incomplete, result["venues"]["exact"]["estimates"][1]):
        assert cell["top_of_book_price"] is None and cell["top_of_book_price_usd"] is None
        assert cell["fee_per_coin"] is None and cell["fee_per_coin_usd"] is None


@pytest.mark.parametrize("change,status", [
    ({"venue_at": NOW - timedelta(seconds=11)}, "stale"),
    ({"venue_at": NOW + timedelta(seconds=6)}, "invalid_timestamp"),
    ({"venue_at": NOW.replace(tzinfo=None)}, "invalid_timestamp"),
    ({"asks": (BookLevel(D("98"), D("1000")),)}, "invalid_book"),
    ({"asks": (BookLevel(D("102"), D("10")), BookLevel(D("101"), D("1000")))}, "invalid_book"),
    ({"bids": (BookLevel(D("NaN"), D("1000")),)}, "invalid_book"),
    ({"asks": (BookLevel(D("101"), D("0")),)}, "invalid_book"),
])
def test_unusable_source_books_cannot_set_reference_or_win(change, status):
    good = book("good")
    invalid = replace(book("invalid"), **change)
    result = execution_estimates([good, invalid], Side.BUY, {"good": D("0"), "invalid": D("0")}, {}, NOW)
    assert D(result["reference_price_usd"]) == 100
    assert first(result, "invalid")["status"] == status
    assert first(result, "invalid")["effective_price_usd"] is None
    assert first(result, "invalid")["all_in_cost_bps"] is None
    assert first(result, "invalid")["top_of_book_price"] is None
    assert first(result, "invalid")["top_of_book_price_usd"] is None
    assert first(result, "invalid")["fee_per_coin"] is None
    assert first(result, "invalid")["fee_per_coin_usd"] is None
    assert not first(result, "invalid")["best"] and first(result, "good")["best"]


@pytest.mark.parametrize("side,unknown_top,fx_top,native_fee", [
    (Side.BUY, "100", "101", "0.0505"),
    (Side.SELL, "98", "99", "0.0495"),
])
def test_unknown_fee_or_fx_cannot_be_ranked_as_a_zero_cost_price(side, unknown_top, fx_top, native_fee):
    depths = [book("good"), book("no_fee", bids=(("98", "2000000"),), asks=(("100", "2000000"),)),
              book("no_fx", quote="USDT")]
    result = execution_estimates(depths, side, {"good": D("5"), "no_fee": None, "no_fx": D("5")}, {}, NOW)
    unknown_fee = first(result, "no_fee")
    assert unknown_fee["status"] == "fee_unknown"
    assert D(unknown_fee["average_price_usd"]) == D(unknown_top)
    assert D(unknown_fee["top_of_book_price"]) == D(unknown_top)
    assert D(unknown_fee["top_of_book_price_usd"]) == D(unknown_top)
    assert unknown_fee["fee_per_coin"] is None and unknown_fee["fee_per_coin_usd"] is None
    assert unknown_fee["effective_price_usd"] is None and unknown_fee["fee_usd"] is None
    assert first(result, "no_fx")["status"] == "fx_missing"
    assert first(result, "no_fx")["average_price"] is not None
    assert first(result, "no_fx")["average_price_usd"] is None
    assert D(first(result, "no_fx")["top_of_book_price"]) == D(fx_top)
    assert D(first(result, "no_fx")["fee_per_coin"]) == D(native_fee)
    assert first(result, "no_fx")["top_of_book_price_usd"] is None
    assert first(result, "no_fx")["fee_per_coin_usd"] is None
    assert first(result, "no_fx")["fee_usd"] is None
    assert unknown_fee["all_in_cost_bps"] is None
    assert D(first(result, "no_fx")["all_in_cost_bps"]) == 5
    assert first(result, "good")["best"] and not unknown_fee["best"] and not first(result, "no_fx")["best"]


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_equal_fee_adjusted_prices_are_tied_best(side):
    result = execution_estimates([book("a"), book("b")], side, {"a": D("5"), "b": D("5")}, {}, NOW)
    assert first(result, "a")["best"] and first(result, "b")["best"]


@pytest.mark.parametrize("limits,expected", [
    ({"max_quantity": D("100")}, "ok"),
    ({"max_quantity": D("99.999")}, "order_limit"),
    ({"max_quote_notional": D("10100")}, "ok"),
    ({"max_quote_notional": D("10099.99")}, "order_limit"),
])
def test_known_market_order_caps_are_not_confused_with_visible_depth(limits, expected):
    result = execution_estimates([book(**limits)], Side.BUY, {"a": D("0")}, {}, NOW)
    cell = first(result)
    assert cell["status"] == expected
    assert cell["best"] == (expected == "ok")
    if expected != "ok":
        assert cell["effective_price_usd"] is None
        assert cell["all_in_cost_bps"] is None
        assert cell["top_of_book_price"] is None and cell["top_of_book_price_usd"] is None
        assert cell["fee_per_coin"] is None and cell["fee_per_coin_usd"] is None
    else:
        assert D(cell["top_of_book_price"]) == 101
        assert D(cell["top_of_book_price_usd"]) == 101
        assert D(cell["fee_per_coin"]) == 0 and D(cell["fee_per_coin_usd"]) == 0


def test_empty_or_one_sided_books_need_an_independent_reference():
    one_sided = book(bids=())
    result = execution_estimates([one_sided], Side.BUY, {"a": D("0")}, {}, NOW)
    assert result["reference_price_usd"] is None and first(result)["status"] == "no_reference"
    assert first(result)["all_in_cost_bps"] is None
    assert first(result)["top_of_book_price"] is None and first(result)["top_of_book_price_usd"] is None
    assert first(result)["fee_per_coin"] is None and first(result)["fee_per_coin_usd"] is None
    result = execution_estimates([one_sided, book("good")], Side.SELL, {"a": D("0"), "good": D("0")}, {}, NOW)
    assert first(result)["status"] == "insufficient_depth" and D(first(result)["filled_quantity"]) == 0
    assert not first(result)["best"]
    assert first(result)["all_in_cost_bps"] is None
    assert first(result)["top_of_book_price"] is None and first(result)["top_of_book_price_usd"] is None
    assert first(result)["fee_per_coin"] is None and first(result)["fee_per_coin_usd"] is None


@pytest.mark.parametrize("side,bounds,fee,venue,expected", [
    (Side.BUY, (("89", "91"), ("109", "111")), "10", "a", "10"),
    (Side.SELL, (("89", "91"), ("109", "111")), "10", "b", "10"),
    (Side.BUY, (("98", "100"), ("100", "102")), "0", "a", "0"),
    (Side.SELL, (("98", "100"), ("100", "102")), "0", "b", "0"),
])
def test_all_in_cost_excludes_venue_price_differences_and_preserves_zero(side, bounds, fee, venue, expected):
    depths = [
        book(name, bids=((bid, "2000000"),), asks=((ask, "2000000"),))
        for name, (bid, ask) in zip(("a", "b"), bounds)
    ]
    result = execution_estimates(depths, side, {"a": D(fee), "b": D(fee)}, {}, NOW)
    assert D(result["reference_price_usd"]) == 100
    cell = first(result, venue)
    assert cell["status"] == "ok" and cell["complete"] and cell["best"]
    assert D(cell["all_in_cost_bps"]) == D(expected)


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_six_bps_fee_plus_four_bps_slippage_is_ten_bps_total(side):
    depth = book(bids=(("100", "50"), ("99.92", "50")),
                 asks=(("100", "50"), ("100.08", "50")))
    cell = first(execution_estimates([depth], side, {"a": D("6")}, {}, NOW))
    assert D(cell["price_impact_bps"]) == 4
    assert D(cell["all_in_cost_bps"]) == 10
    assert D(cell["top_of_book_price"]) == 100 and D(cell["top_of_book_price_usd"]) == 100
    expected_fee = D("0.060024") if side == Side.BUY else D("0.059976")
    assert D(cell["fee_per_coin"]) == expected_fee
    assert D(cell["fee_per_coin_usd"]) == expected_fee
    assert D(cell["fee_usd"]) == expected_fee * 100


def test_round_trip_cost_is_spread_slippage_and_both_fees_while_price_level_cancels():
    # b is cheaper to buy, but a's tighter book is cheaper to get in and out of.
    books = [book("a", bids=(("109", "2000000"),), asks=(("110", "2000000"),)),
             book("b", bids=(("98", "2000000"),), asks=(("100", "2000000"),))]
    fees = {"a": D("10"), "b": D("10")}
    assert first(execution_estimates(books, Side.BUY, fees, {}, NOW), "b")["best"]
    result = round_trip_estimates(books, Side.BUY, fees, {}, NOW)
    a, b = first(result, "a"), first(result, "b")
    quantity = D(a["quantity"])
    # The estimator works at 40 digits; this side multiplies at Python's 28.
    assert abs(D(a["spread_slippage_usd"]) - quantity) < D("1e-20")
    assert abs(D(a["fees_usd"]) - quantity * D("0.219")) < D("1e-20")
    assert abs(D(a["total_cost_usd"]) - quantity * (D("110.11") - D("108.891"))) < D("1e-20")
    assert abs(D(a["total_cost_bps"]) - D("1.219") / D(result["reference_price_usd"]) * 10000) < D("1e-20")
    assert a["best"] and not b["best"]


def test_round_trip_is_as_shallow_as_its_shorter_side():
    shallow_bids = book("a", bids=(("99", "10"),))
    result = round_trip_estimates([shallow_bids, book("b")], Side.BUY, {"a": D("5"), "b": D("5")}, {}, NOW)
    cell = first(result)
    assert cell["status"] == "insufficient_depth" and cell["short_side"] == "sell"
    assert D(cell["fill_percent"]) == D(cell["sell"]["fill_percent"]) == 10
    assert cell["total_cost_usd"] is None and not cell["best"]
    assert first(result, "b")["best"]


def test_held_round_trip_adds_funding_on_the_opening_leg_and_needs_it_to_rank():
    # a's tighter book wins an immediate round trip; holding it costs 20 bps.
    books = [book("a"), book("b", bids=(("98.9", "2000000"),))]
    fees = {"a": D("5"), "b": D("5")}
    assert first(round_trip_estimates(books, Side.BUY, fees, {}, NOW))["best"]
    held = round_trip_estimates(books, Side.BUY, fees, {}, NOW, {"a": D("20"), "b": D("0")})
    a = first(held)
    opening_value = D(a["quantity"]) * D(a["buy"]["average_price_usd"])
    assert abs(D(a["funding_usd"]) - opening_value * D("0.002")) < D("1e-20")
    assert abs(D(a["total_cost_usd"]) - D(a["trading_cost_usd"]) - D(a["funding_usd"])) < D("1e-20")
    assert first(held, "b")["best"] and not a["best"]
    unknown = first(round_trip_estimates(books, Side.BUY, fees, {}, NOW, {"a": None, "b": D("0")}))
    assert unknown["status"] == "funding_unknown" and not unknown["best"]
    assert unknown["total_cost_usd"] is None and unknown["trading_cost_usd"] is not None


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_one_way_holds_rank_on_price_after_funding(side):
    # a has the better price before funding but pays 30 bps to hold.
    books = [book("a"), book("b", bids=(("98.99", "2000000"),), asks=(("101.01", "2000000"),))]
    fees = {"a": D("5"), "b": D("5")}
    assert first(execution_estimates(books, side, fees, {}, NOW))["best"]
    result = execution_estimates(books, side, fees, {}, NOW, {"a": D("30"), "b": D("0")})
    a = first(result)
    factor = 1 + D("0.003") if side == Side.BUY else 1 - D("0.003")
    assert D(a["held_price_usd"]) == D(a["effective_price_usd"]) * factor
    assert D(a["total_cost_bps"]) == D(a["all_in_cost_bps"]) + 30
    assert first(result, "b")["best"] and not a["best"]
    unknown = first(execution_estimates(books, side, fees, {}, NOW, {"a": None, "b": D("0")}))
    assert unknown["status"] == "funding_unknown" and not unknown["best"]
    assert unknown["effective_price_usd"] is not None and unknown["held_price_usd"] is None
