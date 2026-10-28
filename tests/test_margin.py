from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core import execution_fees
from core.execution import BookLevel, DepthBook
from core.margin import (
    BulkRisk, MarginSchedule, MarginTier, Position, bulk_rate, liquidation_price, same_safety,
)
from core.margin_tables import BINANCE_BRACKETS
from data_collection.execution import ExecutionDepth
from web import create_app

D = Decimal


def close(actual, expected, tolerance="1e-9"):
    return abs(D(actual) - D(expected)) < D(tolerance)


def schedule(venue, *tiers):
    return MarginSchedule(venue, "TEST", tuple(tiers), "test")


def bulk(base, low="0.02", high=None, curve=("40", "40", "0", "1"), correlations=None):
    """A BULK market whose rate is `low` everywhere, or falls from `high` at
    1x to `low` at 50x account leverage; `curve` is its leverage curve
    (min, max, notional_0, notional_half)."""
    row = (D(high if high is not None else low), D(low))
    minimum, maximum, start, half = (D(value) for value in curve)
    risk = BulkRisk(base=base, notionals=(D(50_000), D(100_000_000)), leverages=(D(1), D(50)),
                    buy=(row, row), sell=(row, row), leverage_min=minimum, leverage_max=maximum,
                    notional_0=start, notional_half=half,
                    correlations=correlations if correlations is not None else {"BTC:ETH": D("0.7")}, regime=0)
    return MarginSchedule("bulk", f"{base}-USD", (), "test", bulk=risk)


def move_to(price, long, entry=D(100)):
    return None if price is None else ((entry - price) if long else (price - entry)) / entry


# ---- margin rules and the same-safety capital -------------------------------------

@pytest.mark.parametrize("symbol", sorted(BINANCE_BRACKETS))
def test_copied_binance_brackets_keep_maintenance_continuous(symbol):
    # The brackets are typed in by hand: a wrong rate or "cum" breaks continuity.
    rows = [tuple(D(value) for value in row) for row in BINANCE_BRACKETS[symbol]]
    for (cap, leverage, rate, cum), (next_cap, next_leverage, next_rate, next_cum) in zip(rows, rows[1:]):
        assert cap < next_cap and next_leverage <= leverage
        assert cap * rate - cum == cap * next_rate - next_cum


HYPERLIQUID_BTC = schedule("hyperliquid", MarginTier(None, D(40), D("0.0125")))
BULLET = schedule(
    "bullet",
    MarginTier(D(500_000), D(20), D("0.025")),
    MarginTier(D(750_000), D(10), D("0.05"), D(12_500)),
    MarginTier(D(1_000_000), D(5), D("0.10"), D(50_000)),
    MarginTier(D(1_500_000), D(2), D("0.25"), D(200_000)),
    MarginTier(None, D(1), D("0.5"), D(575_000)),
)
EXTENDED = schedule("extended", MarginTier(D(4_000_000), D(50), D("0.01")), MarginTier(D(8_000_000), D(25), D("0.02")))


@pytest.mark.parametrize("venue, long, size, margin, move", [
    # Within one tier, at 10x: (1/L - m) / (1 - m) for a long, (1/L - m) / (1 + m) for a short.
    (HYPERLIQUID_BTC, True, 100_000, 10_000, D("0.0875") / D("0.9875")),
    (HYPERLIQUID_BTC, False, 100_000, 10_000, D("0.0875") / D("1.0125")),
    # A $1m short with $200k rises into the 25% tier: 12%, not the entry tier's 13.6%.
    (BULLET, False, 1_000_000, 200_000, D("0.12")),
    # Extended's maintenance steps up at $4m: the short is liquidated at that edge.
    (EXTENDED, False, 3_900_000, 156_000, D(4) / D("3.9") - 1),
    # A fully funded long cannot be liquidated.
    (HYPERLIQUID_BTC, True, 100_000, 100_000, None),
])
def test_liquidation_follows_the_tier_at_the_liquidation_price(venue, long, size, margin, move):
    quantity, margin = D(size) / 100, D(margin)
    price = liquidation_price(venue.tiers, long, quantity, D(100), margin)
    if move is None:
        assert price is None
        return
    assert close(move_to(price, long), move)
    side = 1 if long else -1

    def short_of_maintenance(at):
        tier = next(t for t in venue.tiers if t.cap is None or quantity * at <= t.cap)
        return margin + side * quantity * (at - 100) < quantity * at * tier.maintenance_rate - tier.maintenance_deduction

    # It is the first such price: a step short of it the position still holds.
    step = D("1e-6")
    assert not short_of_maintenance(price + side * step)
    assert short_of_maintenance(price - side * step)


@pytest.mark.parametrize("venue, long, size, move", [
    (HYPERLIQUID_BTC, True, 100_000, "0.1"),
    (HYPERLIQUID_BTC, False, 100_000, "0.1"),
    (BULLET, True, 700_000, "0.2"),
    # The short rises through Extended's $4m step into its 2% bracket.
    (EXTENDED, False, 3_900_000, "0.05"),
    # A long falling through that step is most at risk just above it, where 2% still applies.
    (EXTENDED, True, 4_200_000, "0.05"),
])
def test_capital_needed_is_the_least_that_survives_the_move(venue, long, size, move):
    quantity, move = D(size) / 100, D(move)
    safety = same_safety([Position(venue, long, quantity, D(100))], move)
    assert not safety.capped

    def survives(capital):
        price = liquidation_price(venue.tiers, long, quantity, D(100), capital)
        return price is None or move_to(price, long) >= move

    assert survives(safety.capital) and not survives(safety.capital - D("0.01"))
    assert close(safety.leverage, D(size) / safety.capital)


def test_a_venues_leverage_cap_sets_the_capital_when_the_move_needs_less():
    # $800k on Bullet allows 5x: $160k, though surviving 10% needs $103.5k.
    capped = same_safety([Position(BULLET, True, D(8_000), D(100))], D("0.1"))
    assert capped.capped and capped.capital == D(160_000) and capped.leverage == 5
    assert move_to(capped.liquidation_price, True) > D("0.1")
    # BULK's curve falls from 40x towards 5x with size: $1m allows 5 + 35 / 1.3 = 31.9x.
    curved = bulk("BTC", curve=("5", "40", "250000", "2500000"))
    tight = same_safety([Position(curved, True, D(10_000), D(100))], D("0.01"))
    assert tight.capped and close(tight.capital, D(1_000_000) / (D(5) + D(35) / D("1.3")), "0.01")


def test_bulk_rate_interpolates_between_knots_and_clamps_beyond_them():
    risk = BulkRisk(base="BTC", notionals=(D(100_000), D(200_000)), leverages=(D(1), D(3)),
                    buy=((D("0.5"), D("0.1")), (D("0.5"), D("0.3"))),
                    sell=((D("0.4"), D("0.2")), (D("0.4"), D("0.2"))),
                    leverage_min=D(5), leverage_max=D(40), notional_0=D(0), notional_half=D(1),
                    correlations={}, regime=0)
    assert bulk_rate(risk, True, D(150_000), D(2)) == D("0.35")
    assert bulk_rate(risk, True, D(10_000_000), D(40)) == D("0.3")
    assert bulk_rate(risk, True, D(1), D("0.5")) == D("0.5")
    assert bulk_rate(risk, False, D(150_000), D(2)) == D("0.3")


def test_bulk_hedges_need_less_capital_than_their_legs_and_standard_venues_add_up():
    move = D("0.1")
    long_btc = Position(bulk("BTC"), True, D(1_000), D(100))
    hedge = same_safety([long_btc, Position(bulk("ETH"), False, D(1_000), D(100))], move)
    # The $20k loss plus +2% x $90k and -2% x $110k combined at rho 0.7.
    assert close(hedge.capital, D(20_000) + D(1_000) * D("2.536").sqrt(), "0.01")
    assert hedge.alone == D(24_000)
    same_way = same_safety([long_btc, Position(bulk("ETH"), True, D(1_000), D(100))], move)
    assert close(same_way.capital, D(20_000) + D(1_800) * D("3.4").sqrt(), "0.01")
    # A negative correlation (one matrix, published with both markets) offsets same-way legs instead.
    inverse = {"BTC:ETH": D("-0.5")}
    offset = same_safety([Position(bulk("BTC", correlations=inverse), True, D(1_000), D(100)),
                          Position(bulk("ETH", correlations=inverse), True, D(1_000), D(100))], move)
    assert close(offset.capital, D(21_800), "0.01")
    # Elsewhere the legs add up, to the cent, though each one rounds separately.
    pair = same_safety([Position(HYPERLIQUID_BTC, True, D("1.2345678"), D("81234.567")),
                        Position(HYPERLIQUID_BTC, False, D("41.234567"), D("2412.3457"))], move)
    assert pair.capital == pair.alone


def test_bulk_capital_covers_the_maintenance_its_own_leverage_implies():
    # Rates fall from 50% at 1x to 2% at 50x account leverage, as BULK's
    # surfaces do for large positions, so more capital raises the rate.
    market = bulk("BTC", high="0.5")
    capital = same_safety([Position(market, True, D(10_000), D(100))], D("0.1")).capital

    def short(posted):
        moved = D(900_000)
        return D(100_000) + bulk_rate(market.bulk, True, moved, moved / posted) * moved > posted

    assert not short(capital) and short(capital - 1)


# ---- each venue's published rules, as parsed --------------------------------------

class HTTP:
    def __init__(self, responses):
        self.responses = responses

    async def get_json(self, url, **kwargs):
        for marker, payload in self.responses.items():
            if marker in url:
                return payload
        raise AssertionError(f"Unexpected public request: {url}")

    async def post_json(self, url, **kwargs):
        return self.responses[kwargs["json"]["type"]]


def provider(responses):
    http = HTTP(responses)
    return ExecutionDepth({venue: http for venue in (
        "binance_perp", "bybit_perp", "hyperliquid", "lighter", "extended", "rise", "bullet", "bulk")})


HYPERLIQUID_META = {"metaAndAssetCtxs": [{
    "universe": [{"name": "BTC", "maxLeverage": 40, "marginTableId": 56},
                 {"name": "SOL", "maxLeverage": 20, "marginTableId": 20}],
    "marginTables": [[56, {"description": "tiered 40x", "marginTiers": [
        {"lowerBound": "0.0", "maxLeverage": 40}, {"lowerBound": "150000000.0", "maxLeverage": 20}]}]],
}, [{}, {}]]}


@pytest.mark.asyncio
@pytest.mark.parametrize("venue, symbol, responses, notional, max_leverage, maintenance", [
    # Fractions are in 1/10,000ths of notional.
    ("lighter", "BTC", {"/orderBookDetails": {"order_book_details": [{
        "symbol": "BTC", "market_id": 1, "min_initial_margin_fraction": 200, "default_initial_margin_fraction": 500,
        "maintenance_margin_fraction": 120, "closeout_margin_fraction": 80}]}},
     100_000, 50, 1_200),
    # The maintenance factor is a reciprocal: 1 / 37.5.
    ("rise", "BTC/USDC", {"/markets": {"data": {"markets": [{"market_id": "1", "config": {
        "name": "BTC/USDC", "max_leverage": "25", "maintenance_margin_factor": "37.5"}}]}}},
     75_000, 25, 2_000),
    # Risk factor = minimum initial margin rate; maintenance is half of it. Unsorted input.
    ("extended", "BTC-USD", {"/info/markets": {"status": "OK", "data": [{"name": "BTC-USD", "tradingConfig": {
        "riskFactorConfig": [{"upperBound": "8000000", "riskFactor": "0.04"}, {"upperBound": "4000000", "riskFactor": "0.02"},
                             {"upperBound": "12000000", "riskFactor": "0.06", "isAvailableForUsers": False}]}}]}},
     5_000_000, 25, 100_000),
    ("extended", "BTC-USD", {"/info/markets": {"status": "OK", "data": [{"name": "BTC-USD", "tradingConfig": {
        "riskFactorConfig": [{"upperBound": "4000000", "riskFactor": "0.02"},
                             {"upperBound": "12000000", "riskFactor": "0.06", "isAvailableForUsers": False}]}}]}},
     10_000_000, None, None),
    # Tier 1's deduction is an empty string; tiers arrive out of order.
    ("bybit_perp", "BTC/USDT", {"/risk-limit": {"retCode": 0, "result": {"list": [
        {"symbol": "BTCUSDT", "riskLimitValue": "2000000", "maintenanceMargin": "0.005", "maxLeverage": "100.00", "mmDeduction": "510"},
        {"symbol": "BTCUSDT", "riskLimitValue": "300000", "maintenanceMargin": "0.0033", "maxLeverage": "150.00", "mmDeduction": ""}]}}},
     1_000_000, 100, 4_490),
    # Bullet's last cap is a 1.8e308 "no limit" sentinel.
    ("bullet", "BTC-USD", {"/leverageBracket": [{"symbol": "BTC-USD", "brackets": [
        {"bracket": 1, "initialLeverage": 20, "notionalFloor": 0, "notionalCap": 500000, "maintMarginRatio": "0.025", "cum": 0},
        {"bracket": 2, "initialLeverage": 1, "notionalFloor": 500000, "notionalCap": "1.7976931348623157E+308",
         "maintMarginRatio": "0.05", "cum": "12500"}]}]},
     10_000_000, 1, 487_500),
    # Maintenance is half the initial rate at each tier's maximum, with
    # deductions keeping it continuous: 150m x (2.5% - 1.25%) = 1.875m.
    ("hyperliquid", "BTC", HYPERLIQUID_META, 200_000_000, 20, 3_125_000),
    ("hyperliquid", "BTC", HYPERLIQUID_META, 100_000, 40, 1_250),
    # Table ids below 50 are one tier at that leverage.
    ("hyperliquid", "SOL", HYPERLIQUID_META, 100_000, 20, 2_500),
])
async def test_public_margin_rules_parse_into_leverage_caps_and_maintenance(
        venue, symbol, responses, notional, max_leverage, maintenance):
    rules = await provider(responses).margin(venue, symbol)
    safety = same_safety([Position(rules, True, D(notional) / 100, D(100))], D("0.1"))
    if max_leverage is None:
        assert safety is None
        return
    assert safety.legs[0].max_leverage == max_leverage
    assert close(safety.legs[0].maintenance, maintenance, "1e-6")


@pytest.mark.asyncio
async def test_bulk_rules_use_the_tickers_regime_aged_rates_and_negative_correlations():
    def cell(origin, end, p):
        return {"mmrO": origin, "mmrE": end, "p": p}
    def surface(regime, value, p):
        row = [cell(value, "0.02", p), cell(value, "0.02", p)]
        return {"regime": regime, "leverage": [1, 50], "notionals": [50000, 100000000], "buy": [row, row], "sell": [row, row]}
    responses = {
        "/riskSurfaces": {"symbol": "BTC-USD", "liveRegime": 10,
                          "leverage": {"min": 5, "max": 40, "notional_0": 250000, "notional_half": 2500000},
                          "surfaces": [surface(0, "0.04", "0.5"), surface(10, "0.5", "0"), surface(1, "0.06", "0")],
                          "corrs": [["BTC:ETH", "0.7"], ["BTC:PAXG", "-0.2"]]},
        "/ticker/": {"regime": 0, "regimeDt": 10},
    }

    async def rate():
        rules = await provider(responses).margin("bulk", "BTC-USD")
        return rules, same_safety([Position(rules, True, D(1_000), D(100))], D("0.1")).legs[0].maintenance_rate

    rules, current = await rate()
    # The ticker's current regime (0), aged 10 steps from 4% towards 2%.
    assert close(current, D("0.02") + D("0.02") * D("0.5") ** 10)
    assert rules.bulk.correlations["BTC:PAXG"] == D("-0.2")
    # A decay of 0 reads as 0.99, as in BULK's own calculator.
    responses["/ticker/"] = {"regime": 1, "regimeDt": 10}
    _rules, current = await rate()
    assert close(current, D("0.02") + D("0.04") * D("0.99") ** 10)


# ---- the calculator API -------------------------------------------------------------

def fee_schedule(label, taker):
    return {"label": label, "default_tier": "base", "source_url": "https://example.invalid/fees",
            "checked_at": "2026-10-19", "notes": [], "tiers": [{"id": "base", "label": "Base", "taker_bps": taker}]}


class MarginReader:
    """Identical books everywhere, so venues differ only by fee and margin
    rules: Binance has the lowest fee and the highest maintenance rate."""
    missing: set[str] = set()

    def capture(self, listings, *, margin=False):
        assert margin
        now = datetime.now(timezone.utc)
        books, margins = [], {}
        for venue, symbol in listings.items():
            books.append(DepthBook(venue, symbol, "USD", now, (BookLevel(D("99.99"), D("10000000")),),
                                   (BookLevel(D("100.01"), D("10000000")),)))
            base = symbol.replace("/", "-").split("-")[0]
            if venue not in self.missing:
                margins[venue] = {
                    "binance_perp": schedule("binance_perp", MarginTier(None, D(20), D("0.05"))),
                    "bybit_perp": schedule("bybit_perp", MarginTier(None, D(100), D("0.005"))),
                    "bulk": bulk(base),
                }[venue]
        return {"books": books, "errors": {}, "margins": margins,
                "margin_errors": {venue: "Public margin data is unavailable from this provider." for venue in self.missing},
                "fx": {"rates": {"USD": "1"}, "ts": now.isoformat(), "source": "Controlled FX",
                       "source_url": "https://example.invalid/fx", "error": None}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OVERSEER_MOCK", "1")
    monkeypatch.setenv("FLASK_SECRET_KEY", "margin-test")
    monkeypatch.delenv("OVERSEER_PROXY_HOPS", raising=False)
    monkeypatch.delenv("OVERSEER_SECURE_COOKIES", raising=False)
    monkeypatch.setattr(execution_fees, "FEE_SCHEDULES", {
        "binance_perp": fee_schedule("Binance", "2"), "bybit_perp": fee_schedule("Bybit", "5"),
        "bulk": fee_schedule("BULK", "3")})
    app = create_app()
    app.config["TESTING"] = True
    real_reader = app.extensions["execution_reader"]
    app.extensions["execution_reader"] = MarginReader()
    # Funding is flat, so holds cost only capital.
    store = app.extensions["read_storage"]
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(store, "current_funding_rates", lambda exchanges, symbols: [
        {"exchange": exchange, "symbol": symbol, "ts": now, "rate": D(0), "interval_hours": 8}
        for exchange in exchanges for symbol in symbols])
    monkeypatch.setattr(store, "settled_funding_rates", lambda exchanges, symbols, since: [
        {"exchange": exchange, "symbol": symbol, "ts": now - timedelta(hours=1 + 8 * step), "rate": D(0), "interval_hours": 8}
        for exchange in exchanges for symbol in symbols for step in range(3)])
    yield app.test_client()
    real_reader.close()
    app.extensions["position_reader"].close()


def venues(payload):
    return {row["venue"]: row for row in payload["venues"]}


def best(rows):
    return [venue for venue, row in rows.items() if row["best"]]


def btc(size="100000", direction="long"):
    return {"asset": "BTC", "direction": direction, "size": size}


def test_capital_cost_counts_once_a_rate_is_set(client):
    free = venues(client.post("/api/margin", json={"position": btc(), "move": "10"}).get_json())
    assert best(free) == ["binance_perp"]
    # A month at 100% a year: Binance's 5% maintenance needs the most capital.
    priced = venues(client.post("/api/margin", json={
        "position": btc(), "move": "10", "rate": "100", "hold_hours": 720}).get_json())
    assert best(priced) == ["bybit_perp"]
    for row in priced.values():
        costs = {key: D(value) for key, value in row["costs"].items()}
        assert close(costs["capital_usd"], D(row["combined"]["capital_usd"]) * 720 / 8760)
        assert close(costs["total_usd"], costs["trading_usd"] + costs["funding_usd"] + costs["capital_usd"])


def test_least_capital_ranks_capital_needed_and_needs_published_rules(client):
    rows = venues(client.post("/api/margin", json={"position": btc(), "rank": "capital"}).get_json())
    assert best(rows) == ["bybit_perp"]
    MarginReader.missing = {"bybit_perp"}
    try:
        unknown = venues(client.post("/api/margin", json={"position": btc(), "rank": "capital"}).get_json())
    finally:
        MarginReader.missing = set()
    assert unknown["bybit_perp"]["status"] == "margin_unavailable" and unknown["bybit_perp"]["combined"] is None
    assert best(unknown) == ["bulk"]


def test_a_hedge_needs_less_capital_than_its_legs_only_on_bulk(client):
    rows = venues(client.post("/api/margin", json={
        "position": btc(), "pair": {"asset": "ETH", "direction": "short", "size": "100000"}, "move": "10"}).get_json())
    hedge = rows["bulk"]["combined"]
    assert D(hedge["capital_usd"]) < D(hedge["alone_usd"]) * D("0.92")
    assert close(hedge["offset_usd"], D(hedge["alone_usd"]) - D(hedge["capital_usd"]))
    for venue in ("binance_perp", "bybit_perp"):
        combined = rows[venue]["combined"]
        assert D(combined["capital_usd"]) == D(combined["alone_usd"]) and D(combined["offset_usd"]) == 0


@pytest.mark.parametrize("body", [
    {"position": btc(), "pair": btc(direction="short")},
    {"position": btc(size=1000.5)},
    {"position": btc(size="50")},
    {"position": {"asset": "BTC", "direction": "up", "size": "1000"}},
    {"position": btc(), "move": "0"},
    {"position": btc(), "move": "150"},
    {"position": btc(), "move": 10.5},
    {"position": btc(), "leverage": "10"},
    {"position": btc(), "hold_hours": 5},
    {"position": btc(), "rank": "fees"},
    {"position": btc(), "rate": "-1"},
    {"position": btc(), "fees": {"unknown": "base"}},
    {"move": "10"},
])
def test_invalid_requests_are_rejected(client, body):
    response = client.post("/api/margin", json=body)
    assert response.status_code == 400 and "error" in response.get_json()
