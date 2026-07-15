from __future__ import annotations

from enum import StrEnum


class Exchange(StrEnum):
    BINANCE = "binance"
    HYPERLIQUID = "hyperliquid"
    BYBIT = "bybit"
    LIGHTER = "lighter"
    EXTENDED = "extended"
    BULK = "bulk"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class MarketType(StrEnum):
    SPOT = "spot"
    PERP = "perp"          # perpetual swap
    FUTURE = "future"
    OPTION = "option"


class Timeframe(StrEnum):
    """Bar intervals. The value is the literal string both Binance and
    Hyperliquid expect in their requests, so no per-venue interval mapping is
    needed for the common set."""
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H8 = "8h"
    H12 = "12h"
    D1 = "1d"