from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from core.enums import Exchange,Side,MarketType,Timeframe

@dataclass(frozen=True, slots=True)
class Trade:
    exchange: Exchange
    market_type: MarketType 
    symbol: str         
    trade_id: str        
    price: Decimal
    amount: Decimal
    side: Side
    ts: datetime
    wallet_address: str | None

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
