from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.enums import Exchange, MarketType, QuoteCurrency, Side, Timeframe


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
    """Point-in-time venue state for a perp.

    OI is in base units; volume_24h is quote notional. Current funding fields
    are the venue-published rate for the next settlement, not settled history.
    They remain nullable because not every venue exposes every input."""

    exchange: Exchange
    symbol: str
    ts: datetime                 # snapshot time (our clock)
    open_interest: Decimal       # base units
    volume_24h: Decimal          # quote notional
    mark_price: Decimal
    index_price: Decimal | None = None
    current_funding_rate: Decimal | None = None
    funding_interval_hours: int | None = None
    next_funding_at: datetime | None = None
    funding_premium: Decimal | None = None

    @property
    def key(self) -> tuple[str, str, datetime]:
        return (self.exchange, self.symbol, self.ts)


@dataclass(frozen=True, slots=True)
class TopOfBook:
    """One point-in-time executable quote for a venue market."""

    exchange: Exchange
    symbol: str
    quote_currency: QuoteCurrency
    ts: datetime
    bid_price: Decimal
    bid_size: Decimal
    ask_price: Decimal
    ask_size: Decimal


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    """Persisted executable top-of-book quote for spread history."""

    exchange: Exchange
    market_type: MarketType
    symbol: str
    quote_currency: QuoteCurrency
    ts: datetime
    bid_price: Decimal
    bid_size: Decimal
    ask_price: Decimal
    ask_size: Decimal

    @property
    def key(self) -> tuple[str, str, str, datetime]:
        return (self.exchange, self.market_type, self.symbol, self.ts)


@dataclass(frozen=True, slots=True)
class TradeFlow:
    """One minute of order flow for one market, aggregated in-process.

    Computed as trades arrive rather than derived from stored rows, because
    the raw tape is deliberately NOT persisted outside funding-settlement
    windows — measured at ~1.35 GB/day for five assets on two venues, and the
    row count tracks how much the market trades rather than how many symbols
    we follow, so it scales unpredictably. This is the opposite: exactly 1440
    rows per market per day whatever the market does.

    open/close are first/last BY TRADE TIME, not arrival order — a venue can
    deliver slightly out of order, and on a reconnect it can replay.
    """

    exchange: Exchange
    market_type: MarketType
    symbol: str
    bucket: datetime             # minute start, tz-aware UTC
    trades: int
    volume: Decimal              # base units
    buy_volume: Decimal          # taker-buy base units
    sell_volume: Decimal
    notional: Decimal            # sum(price * amount), quote
    # Size participation. Volume and trade count together cannot distinguish
    # one institutional clip from a thousand retail ones, and around a funding
    # settlement that is the whole question — is this real positioning or noise.
    large_trades: int            # count at or above LARGE_TRADE_NOTIONAL
    large_buy_volume: Decimal    # base units, large trades only
    large_sell_volume: Decimal
    max_trade_notional: Decimal  # biggest single trade, threshold-free
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    @property
    def large_imbalance(self) -> Decimal | None:
        """Directional imbalance among LARGE trades only. Diverging from the
        all-trades imbalance is the interesting case: retail leaning one way
        while size leans the other."""
        total = self.large_buy_volume + self.large_sell_volume
        if not total:
            return None
        return (self.large_buy_volume - self.large_sell_volume) / total

    @property
    def imbalance(self) -> Decimal | None:
        """(buys - sells) / volume, in [-1, 1]. Positive when takers are
        lifting offers — the directional-pressure signal that shows flow
        building ahead of a funding settlement."""
        if not self.volume:
            return None
        return (self.buy_volume - self.sell_volume) / self.volume

    @property
    def key(self) -> tuple[str, str, str, datetime]:
        return (self.exchange, self.market_type, self.symbol, self.bucket)


@dataclass(frozen=True, slots=True)
class LeverageStats:
    """Pool TVL + effective leverage for one leveraged token, per bar.

    NAV is not here — it arrives as OHLC and is stored in `ohlcv` under
    market_type = LEVERAGED_TOKEN. Both fields are optional because they come
    from two separate endpoints whose bar alignment is not yet confirmed; a
    record is emitted when either side has a value.
    """

    exchange: Exchange
    symbol: str                  # token symbol, e.g. "xBTC"
    interval: Timeframe
    ts: datetime                 # bar open time, UTC
    total_value: Decimal | None  # collateral pool TVL, quote notional
    leverage: Decimal | None     # ASSET TVL / xASSET market cap

    @property
    def collateral_ratio(self) -> Decimal | None:
        """CR implied by effective leverage.

        Both are ratios on the same two quantities: with pool value C and
        stablecoin liability D, leverage L = C / (C - D) and CR = C / D, so
        CR = L / (L - 1). Sanity check against Hylo's published target — 3x
        leverage gives 1.5, i.e. the 150% CR they aim for.

        None below L <= 1, where the identity breaks down: that means the pool
        carries no stablecoin liability at all, so there is no ratio to report.
        """
        if self.leverage is None or self.leverage <= 1:
            return None
        return self.leverage / (self.leverage - 1)

    @property
    def key(self) -> tuple[str, str, str, datetime]:
        return (self.exchange, self.symbol, self.interval, self.ts)


@dataclass(frozen=True, slots=True)
class VenueVolume:
    """Venue-WIDE 24h quote volume (all markets, not just tracked assets).
    Collected once daily; spot/perp splits are nullable where a venue has one
    side only. Feeds the CEX-vs-DEX share widget."""

    exchange: Exchange
    ts: datetime
    volume_total: Decimal
    volume_spot: Decimal | None
    volume_perp: Decimal | None

    @property
    def key(self) -> tuple[str, datetime]:
        return (self.exchange, self.ts)