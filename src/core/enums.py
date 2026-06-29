from enum import StrEnum

class Exchange(StrEnum):
    Hyperliquid = "hyperliquid"
    Extended = "extended"
    Phoenix = "phoenix"
    Bulktrade = "bulktrade"
    Lighter = "lighter"
    Binance = "binance"
    Coinbase = "coinbase"
    CME = "cme"

class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

class MarketType(StrEnum):
    SPOT = "spot"
    PERP = "perp"
    FUTURE = "future"
    OPTION = "option"

class Timeframe(StrEnum):
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
    W1 = "1w"