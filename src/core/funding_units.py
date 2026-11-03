"""Spot a venue whose funding rates are off by a unit factor.

Most venues charge a fixed interest component, so their quiet crypto markets
sit on one exact resting rate (0.01% per eight hours on most venues: 10.95% a
year). A unit error, such as an eight-hour rate stored as hourly or a
percentage stored as a fraction, scales every rate a venue reports, resting
rate included, while real dislocations move individual markets. Venues' crypto
resting rates agree within a few per cent (10.51% to 11.39% a year when this
was written), so a unit error lands a whole factor away. Comparing each market
with other venues instead would mistake a venue that genuinely pays more for an
error. Callers pass crypto markets only: traditional ones rest on lower,
venue-specific rates (3.50% to 5.48% when checked). A venue whose markets
share no resting rate is not judged.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

RESTING_MARKETS = 3    # markets sharing one exact positive rate before it is the venue's resting rate
REFERENCE_VENUES = 2   # other venues with a resting rate needed to compare against
UNIT_ERROR_FACTOR = 2  # a resting rate this far from the others' median is a unit error


@dataclass(frozen=True, slots=True)
class RestingRate:
    exchange: str
    apr_pct: float        # the venue's resting rate, annualised
    markets: int          # markets sitting on it
    reference_pct: float  # median of the other venues' resting rates

    @property
    def factor(self) -> float:
        return self.apr_pct / self.reference_pct

    @property
    def suspect(self) -> bool:
        return not 1 / UNIT_ERROR_FACTOR < self.factor < UNIT_ERROR_FACTOR


def _apr(rate: Decimal, hours: int) -> float:
    return float(rate) * 8760 / hours * 100


def resting_rates(rates: Iterable[tuple[str, Decimal, int]]) -> list[RestingRate]:
    """Each judged venue's resting rate against the other venues', from one
    (exchange, per-interval rate, interval hours) per market."""
    shared: dict[str, Counter[tuple[Decimal, int]]] = defaultdict(Counter)
    for exchange, rate, hours in rates:
        if rate > 0 and hours > 0:
            shared[exchange][(rate, hours)] += 1
    resting: dict[str, tuple[float, int]] = {}
    for exchange, counts in shared.items():
        # The most shared rate; on a tie the lower, a floor rather than a cap.
        (rate, hours), markets = max(counts.items(), key=lambda item: (item[1], -_apr(*item[0])))
        if markets >= RESTING_MARKETS:
            resting[exchange] = (_apr(rate, hours), markets)
    judged = []
    for exchange, (apr, markets) in sorted(resting.items()):
        others = [other for venue, (other, _) in resting.items() if venue != exchange]
        if len(others) >= REFERENCE_VENUES:
            judged.append(RestingRate(exchange, apr, markets, median(others)))
    return judged
