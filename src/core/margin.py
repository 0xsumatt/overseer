"""Perpetual margin for the margin calculator: the capital each venue needs to
keep a position, or a pair of positions, open through a chosen adverse move.

Most venues publish notional tiers. Each tier caps leverage and sets the
maintenance rate; a deduction keeps maintenance margin continuous across
tiers (Binance "cum", Bybit "mmDeduction", Hyperliquid
"maintenance_deduction"):

    maintenance margin = notional x maintenance_rate - maintenance_deduction

and two positions need the sum of their maintenance margins. BULK instead
publishes portfolio-margin inputs: maintenance rates by position size and
account leverage for each volatility regime, a maximum-leverage curve and
correlations between markets, so a hedged pair can need less than its legs.

At the same leverage every venue asks for the same margin, so venues are
compared at the same safety instead: the capital needed is the loss at the
move plus maintenance margin at the moved prices, and never less than the
venue's minimum, notional / maximum leverage.

Notional and margin are in the venue's own quote currency. A tier is chosen by
the whole position's notional, inclusive of its cap.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class MarginTier:
    cap: Decimal | None              # largest notional in this tier; None means unbounded
    max_leverage: Decimal
    maintenance_rate: Decimal        # fraction of notional
    maintenance_deduction: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class BulkRisk:
    """BULK's published risk inputs for one market, at its live regime.

    Grids hold maintenance rates (lambda) already aged to the regime's
    current age: buy[n][l] for longs, sell[n][l] for shorts, at notionals[n]
    and account leverage leverages[l].
    """
    base: str
    notionals: tuple[Decimal, ...]
    leverages: tuple[Decimal, ...]
    buy: tuple[tuple[Decimal, ...], ...]
    sell: tuple[tuple[Decimal, ...], ...]
    leverage_min: Decimal
    leverage_max: Decimal
    notional_0: Decimal
    notional_half: Decimal
    correlations: Mapping[str, Decimal]   # "BTC:ETH" (ASCII order) -> rho
    regime: int


@dataclass(frozen=True, slots=True)
class MarginSchedule:
    venue: str
    symbol: str
    tiers: tuple[MarginTier, ...]    # ascending by cap; empty for BULK
    source: str                      # where the parameters came from, for display
    source_url: str | None = None
    notes: tuple[str, ...] = ()
    bulk: BulkRisk | None = None


@dataclass(frozen=True, slots=True)
class Position:
    """`quantity` coins entered at `entry` (quote currency per coin)."""
    schedule: MarginSchedule
    long: bool
    quantity: Decimal
    entry: Decimal

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.entry

    def moved(self, move: Decimal) -> Decimal:
        """The price after `move` (a fraction of entry) against the position."""
        return self.entry * (1 - move) if self.long else self.entry * (1 + move)


@dataclass(frozen=True, slots=True)
class LegSafety:
    notional: Decimal                # at entry
    max_leverage: Decimal            # at this notional
    loss: Decimal                    # at the moved price
    maintenance: Decimal             # at entry; on BULK at the account's leverage
    maintenance_rate: Decimal
    moved_maintenance: Decimal       # after the move; on BULK before offsets
    alone: Decimal                   # capital this position would need on its own


@dataclass(frozen=True, slots=True)
class Safety:
    legs: tuple[LegSafety, ...]
    capital: Decimal                 # least capital kept above maintenance through the move
    minimum: Decimal                 # the venue's floor: notional / maximum leverage, summed
    capped: bool                     # the floor, not the move, sets the capital
    leverage: Decimal                # gross notional / capital
    loss: Decimal
    moved_maintenance: Decimal       # after the move: portfolio on BULK, else summed
    maintenance: Decimal             # at entry: portfolio on BULK, else summed
    alone: Decimal                   # the legs' own capital needs, summed
    liquidation_price: Decimal | None  # one position, isolated with `capital` posted
    liquidation_move: Decimal | None   # adverse move to that price, as a fraction of entry


def tier_for(schedule: MarginSchedule, notional: Decimal) -> MarginTier | None:
    """The tier holding this notional, or None above the venue's largest tier."""
    for tier in schedule.tiers:
        if tier.cap is None or notional <= tier.cap:
            return tier
    return None


def deductions(rates: Sequence[Decimal], floors: Sequence[Decimal]) -> list[Decimal]:
    """Maintenance deductions that keep a tiered maintenance margin continuous:
    each tier's deduction is the previous one plus its floor times the rise in
    rate (the published Hyperliquid rule, and how Binance's cum is built)."""
    out = [Decimal(0)]
    for index in range(1, len(rates)):
        out.append(out[-1] + floors[index] * (rates[index] - rates[index - 1]))
    return out


def bulk_max_leverage(risk: BulkRisk, notional: Decimal) -> Decimal:
    """BULK's published curve: the maximum falls from leverage_max towards
    leverage_min as notional grows past notional_0."""
    excess = max(Decimal(0), notional - risk.notional_0) / risk.notional_half
    return risk.leverage_min + (risk.leverage_max - risk.leverage_min) / (1 + excess)


def _segment(knots: Sequence[Decimal], value: Decimal) -> tuple[int, int, Decimal]:
    """Neighbouring knot indices and the weight of the upper one, clamped."""
    if value <= knots[0]:
        return 0, 0, Decimal(0)
    if value >= knots[-1]:
        last = len(knots) - 1
        return last, last, Decimal(0)
    upper = next(index for index, knot in enumerate(knots) if knot >= value)
    lower = upper - 1
    return lower, upper, (value - knots[lower]) / (knots[upper] - knots[lower])


def bulk_rate(risk: BulkRisk, long: bool, notional: Decimal, leverage: Decimal) -> Decimal:
    """Maintenance rate at a position's notional and the account's leverage,
    interpolated bilinearly between grid knots as BULK's own calculator does."""
    grid = risk.buy if long else risk.sell
    n0, n1, wn = _segment(risk.notionals, notional)
    l0, l1, wl = _segment(risk.leverages, leverage)
    near = grid[n0][l0] * (1 - wl) + grid[n0][l1] * wl
    far = grid[n1][l0] * (1 - wl) + grid[n1][l1] * wl
    return near * (1 - wn) + far * wn


def max_leverage(schedule: MarginSchedule, notional: Decimal) -> Decimal | None:
    if schedule.bulk is not None:
        return bulk_max_leverage(schedule.bulk, notional)
    tier = tier_for(schedule, notional)
    return tier.max_leverage if tier is not None else None


def liquidation_price(tiers: Sequence[MarginTier], long: bool, quantity: Decimal, entry: Decimal,
                      margin: Decimal) -> Decimal | None:
    """Isolated liquidation, before fees and funding: the first price, moving
    against the position from entry, at which posted margin plus PnL falls
    below the maintenance margin. Maintenance follows the tier of the
    position's value at that price, as Binance and Hyperliquid document, so the
    walk crosses tiers; a short can be liquidated at a tier edge where
    maintenance steps up (Extended). Beyond the last tier its rate continues.
    None means a long that no fall in price can liquidate.

    The walk runs in notional value, where tier edges are exact published
    numbers, so a tie at an edge is not decided by rounding.
    """
    floors = [Decimal(0), *(tier.cap for tier in tiers[:-1])]
    start = quantity * entry
    index = next(i for i, tier in enumerate(tiers) if tier.cap is None or start <= tier.cap)
    side = 1 if long else -1

    def short_of_maintenance(i: int, notional: Decimal) -> bool:
        tier = tiers[i]
        return margin + side * (notional - start) < notional * tier.maintenance_rate - tier.maintenance_deduction

    def crossing(tier: MarginTier) -> Decimal:
        # margin + side x (n - start) = n x rate - deduction
        return (side * start - margin - tier.maintenance_deduction) / (side - tier.maintenance_rate)

    if short_of_maintenance(index, start):
        return entry
    while True:
        notional = crossing(tiers[index])
        if long:
            # A floor belongs to the tier below (caps are inclusive), so a
            # crossing exactly there is decided by that tier.
            if notional > floors[index]:
                return notional / quantity
            if index == 0:
                return None
            edge = floors[index]
            index -= 1
            if short_of_maintenance(index, edge):
                return edge / quantity
        else:
            cap = tiers[index].cap
            if cap is None or index == len(tiers) - 1 or notional <= cap:
                return notional / quantity
            index += 1
            if short_of_maintenance(index, cap):
                return cap / quantity


def _combine(values: Sequence[Decimal], bases: Sequence[str], correlations: Mapping[str, Decimal]) -> Decimal:
    """sqrt(sum v_i^2 + 2 sum v_i v_j rho_ij): BULK's correlated aggregate."""
    def rho(a: str, b: str) -> Decimal:
        if a == b:
            return Decimal(1)
        first, second = sorted((a, b))
        return correlations.get(f"{first}:{second}", Decimal(0))
    total = sum(value * value for value in values)
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            total += 2 * values[i] * values[j] * rho(bases[i], bases[j])
    return max(Decimal(0), total).sqrt()


def _tier_maintenance(position: Position, price: Decimal) -> tuple[Decimal, Decimal]:
    """(maintenance margin, rate) at `price`, in the tier of the position's
    value there; beyond the largest tier its rate continues."""
    notional = position.quantity * price
    tier = tier_for(position.schedule, notional) or position.schedule.tiers[-1]
    return notional * tier.maintenance_rate - tier.maintenance_deduction, tier.maintenance_rate


def _tiered_need(position: Position, move: Decimal) -> Decimal:
    """Loss plus maintenance at the worst point of the move. Within a tier it
    grows with the move, so the worst point is the moved price, or for a long
    whose maintenance steps down at a bracket floor (Extended) just above
    that floor, where the higher bracket still applies."""
    start = position.notional
    need = start * move + _tier_maintenance(position, position.moved(move))[0]
    if position.long:
        tiers = position.schedule.tiers
        for lower, upper in zip(tiers, tiers[1:]):
            if start * (1 - move) < lower.cap < start:
                need = max(need, start - lower.cap + lower.cap * upper.maintenance_rate - upper.maintenance_deduction)
    return need


def _bulk_safety(positions: Sequence[Position], move: Decimal, loss: Decimal, minimum: Decimal):
    """BULK: the least capital C covering loss plus portfolio maintenance at
    the moved prices, where rates depend on account leverage, gross exposure
    net of correlation / C. Rates rise as leverage falls, so the search starts
    from the surface's lowest rates and steps up to the smallest C that covers
    the maintenance its own leverage implies.

    Returns (capital, entry charges, moved charges, entry rates, entry
    portfolio maintenance, moved portfolio maintenance); charges are signed
    lambda x notional per position.
    """
    risks = [p.schedule.bulk for p in positions]
    correlations: dict[str, Decimal] = {}
    for risk in risks:
        correlations.update(risk.correlations)
    bases = [risk.base for risk in risks]
    signs = [1 if p.long else -1 for p in positions]

    def rates(prices, leverage):
        return [bulk_rate(risk, p.long, p.quantity * price, leverage) for p, risk, price in zip(positions, risks, prices)]

    def charges(prices, leverage):
        return [sign * rate * p.quantity * price
                for p, sign, rate, price in zip(positions, signs, rates(prices, leverage), prices)]

    def exposure(prices):
        return _combine([sign * p.quantity * price for p, sign, price in zip(positions, signs, prices)], bases, correlations)

    moved = [p.moved(move) for p in positions]
    moved_exposure = exposure(moved)
    capital = max(minimum, loss + _combine(charges(moved, max(risk.leverages[-1] for risk in risks)), bases, correlations))
    for _ in range(200):
        needed = max(minimum, loss + _combine(charges(moved, moved_exposure / capital), bases, correlations))
        settled = abs(needed - capital) < Decimal("0.005")
        capital = max(capital, needed) if settled else needed
        if settled:
            break
    entry = [p.entry for p in positions]
    entry_leverage = exposure(entry) / capital
    entry_charges = charges(entry, entry_leverage)
    moved_charges = charges(moved, moved_exposure / capital)
    return (capital, entry_charges, moved_charges, rates(entry, entry_leverage),
            _combine(entry_charges, bases, correlations), _combine(moved_charges, bases, correlations))


def _requirement(positions: Sequence[Position], move: Decimal):
    """The unrounded capital and its parts, or None when a position is larger
    than the venue's largest tier."""
    caps = [max_leverage(p.schedule, p.notional) for p in positions]
    if any(cap is None for cap in caps):
        return None
    minimum = sum(p.notional / cap for p, cap in zip(positions, caps))
    losses = [p.notional * move for p in positions]
    loss = sum(losses)
    if positions[0].schedule.bulk is not None:
        capital, entry_charges, moved_charges, entry_rates, maintenance, moved_maintenance = _bulk_safety(
            positions, move, loss, minimum)
        need = loss + moved_maintenance
        entry_each = [abs(charge) for charge in entry_charges]
        moved_each = [abs(charge) for charge in moved_charges]
    else:
        need = sum(_tiered_need(p, move) for p in positions)
        capital = max(minimum, need)
        at_entry = [_tier_maintenance(p, p.entry) for p in positions]
        entry_each, entry_rates = [value for value, _ in at_entry], [rate for _, rate in at_entry]
        moved_each = [_tier_maintenance(p, p.moved(move))[0] for p in positions]
        maintenance, moved_maintenance = sum(entry_each), sum(moved_each)
    return (capital, need, minimum, caps, losses, entry_each, entry_rates, moved_each,
            maintenance, moved_maintenance)


def _round_up(value: Decimal) -> Decimal:
    # Money, and rounding up keeps exact-arithmetic ties (equity equal to
    # maintenance at a bracket edge) on the surviving side.
    return value.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def same_safety(positions: Sequence[Position], move: Decimal) -> Safety | None:
    """The least capital that keeps these positions (one venue, one cross
    account) above maintenance margin while each moves `move` (a fraction of
    entry) against you, and never less than the venue's minimum at its maximum
    leverage. None when a position is larger than the venue's largest tier.

    A single position also gets its isolated liquidation price with that
    capital posted, before fees and funding: at the move, or further when the
    minimum sets the capital. On BULK the rate there is held at its entry value.
    """
    with localcontext() as context:
        context.prec = 40
        found = _requirement(positions, move)
        if found is None:
            return None
        (capital, need, minimum, caps, losses, entry_each, entry_rates, moved_each,
         maintenance, moved_maintenance) = found
        # Legs are summed before rounding, so where they simply add up (every
        # venue but BULK, unless a leverage cap binds) the pair matches them.
        alone = [capital] if len(positions) == 1 else [_requirement([p], move)[0] for p in positions]
        capital = _round_up(capital)
        price = liquidation_move = None
        if len(positions) == 1:
            p = positions[0]
            tiers = (MarginTier(None, caps[0], entry_rates[0]),) if p.schedule.bulk is not None else p.schedule.tiers
            price = liquidation_price(tiers, p.long, p.quantity, p.entry, capital)
            if price is not None:
                liquidation_move = (p.entry - price) / p.entry if p.long else (price - p.entry) / p.entry
        legs = tuple(LegSafety(p.notional, cap, leg_loss, entry_value, rate, moved_value, _round_up(own))
                     for p, cap, leg_loss, entry_value, rate, moved_value, own
                     in zip(positions, caps, losses, entry_each, entry_rates, moved_each, alone))
        return Safety(legs, capital, minimum, minimum >= need, sum(p.notional for p in positions) / capital,
                      sum(losses), moved_maintenance, maintenance, _round_up(sum(alone)), price, liquidation_move)


def capital_cost(capital_usd: Decimal, annual_rate: Decimal, hold_hours: int) -> Decimal:
    """What the capital needed would have earned elsewhere over the hold."""
    return capital_usd * annual_rate * hold_hours / 8760
