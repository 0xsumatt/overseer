from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


class PositionReadError(ValueError):
    """A safe, user-facing account-read error; never contains credentials."""


def number(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise PositionReadError("Venue returned a non-finite account amount.")
    return result


@dataclass(frozen=True, slots=True)
class CurrencyAmount:
    currency: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    quantity: Decimal
    quote_currency: str
    settlement_currency: str
    collateral_currencies: tuple[str, ...]
    mark_price: Decimal | None = None
    entry_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    scope: str = "main"
    pnl_currency: str | None = None


@dataclass(slots=True)
class PositionSnapshot:
    positions: list[Position] = field(default_factory=list)
    balances: list[CurrencyAmount] = field(default_factory=list)
    equity: list[CurrencyAmount] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FundingEvent:
    event_id: str
    ts: datetime
    symbol: str
    amount: Decimal
    currency: str
    scope: str = "main"


@dataclass(slots=True)
class FundingPage:
    events: list[FundingEvent] = field(default_factory=list)
    cursor: dict[str, Any] | None = None
    complete: bool = True
    coverage_since: datetime | None = None
    coverage_note: str | None = None


_QUOTE_SUFFIXES = (
    "FDUSD", "USDT", "USDC", "TUSD", "BUSD", "DAI", "USD",
    "BTC", "ETH", "BNB", "EUR", "TRY", "GBP", "BRL",
)


def asset_id(registry, symbol: str) -> str:
    """Configured aliases first; preserve unknown contract units and namespaces' bases."""
    configured = registry.asset_for(symbol)
    if configured is not None:
        return configured
    market = symbol.split(":", 1)[-1]
    if "/" in market:
        base, quote = market.rsplit("/", 1)
        if base and quote:
            return base
    for quote in _QUOTE_SUFFIXES:
        suffix = f"-{quote}"
        if market.endswith(suffix) and len(market) > len(suffix):
            return market[:-len(suffix)]
    return market
