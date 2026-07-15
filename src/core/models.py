from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.enums import Exchange, MarketType, Side, Timeframe


@dataclass(frozen=True, slots=True)
class Trade:
    exchange: Exchange
    market_type: MarketType
    symbol: str                  # canonical, e.g. "BTC/USDT"
    trade_id: str                # venue-native id → idempotency key
    price: Decimal
    amount: Decimal
    side: Side                   # taker/aggressor side
    ts: datetime                 # event time, tz-aware UTC
    wallet_address: str | None = None   # populated only by on-chain venues

    # natural key for idempotent upserts
    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.exchange, self.market_type, self.symbol, self.trade_id)


@dataclass(frozen=True, slots=True)
class OHLCV:
    exchange: Exchange
    market_type: MarketType
    symbol: str
    interval: Timeframe
    ts: datetime                 # bar open time, tz-aware UTC
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @property
    def key(self) -> tuple[str, str, str, str, datetime]:
        return (self.exchange, self.market_type, self.symbol, self.interval, self.ts)


@dataclass(frozen=True, slots=True)
class FundingRate:
    """A settled funding payment on a perp. Perp-domain only.

    Venues pay on different intervals (Hyperliquid hourly; Binance 8h by
    default but 4h for some symbols), so the raw rate is NOT comparable across
    venues. interval_hours travels with every record; consumers annualize:
    apr = rate * (8760 / interval_hours).
    """

    exchange: Exchange
    symbol: str                  # venue-canonical perp symbol
    ts: datetime                 # funding settlement time, UTC
    rate: Decimal                # per-interval rate (e.g. 0.0001 = 1bp)
    interval_hours: int          # 1 (HL), 8 or 4 (Binance)

    @property
    def key(self) -> tuple[str, str, datetime]:
        return (self.exchange, self.symbol, self.ts)


@dataclass(frozen=True, slots=True)
class LiquiditySnapshot:
    """Point-in-time venue liquidity for a perp — the 'can you actually get
    filled here' context next to a funding number. OI is in BASE units
    (both venues report it that way); notional = open_interest * mark_price.
    volume_24h is quote/notional (USDT / USDC)."""

    exchange: Exchange
    symbol: str
    ts: datetime                 # snapshot time (our clock)
    open_interest: Decimal       # base units
    volume_24h: Decimal          # quote notional
    mark_price: Decimal

    @property
    def key(self) -> tuple[str, str, datetime]:
        return (self.exchange, self.symbol, self.ts)