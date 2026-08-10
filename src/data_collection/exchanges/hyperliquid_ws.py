from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import ClassVar

import orjson

from core.enums import Exchange, Side
from core.models import Trade
from data_collection.base import Capability
from data_collection.exchanges.hyperliquid import HyperliquidScraper
from data_collection.streams import BaseExchangeStream


class HyperliquidTradesStream(BaseExchangeStream):
    exchange: ClassVar[Exchange] = Exchange.HYPERLIQUID
    ws_url: ClassVar[str] = "wss://api.hyperliquid.xyz/ws"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.TRADES})
    kind: ClassVar[str] = "trades"

    def subscribe_frames(self) -> Sequence[bytes]:
        return [
            orjson.dumps(
                {"method": "subscribe",
                 "subscription": {"type": "trades", "coin": coin}}
            )
            for coin in self.symbols
        ]

    def parse(self, payload: bytes) -> Sequence[Trade]:
        message = orjson.loads(payload)
        if message.get("channel") != "trades":
            return ()                    

        data = message.get("data") or []
        if isinstance(data, dict):        
            data = [data]

        out: list[Trade] = []
        for row in data:
            coin = row.get("coin")
            tid = row.get("tid")
            if coin is None or tid is None:
                continue
            out.append(
                Trade(
                    exchange=self.exchange,
                    market_type=HyperliquidScraper.market_type_for(coin),
                    symbol=coin,
                    trade_id=str(tid),
                    price=Decimal(str(row["px"])),
                    amount=Decimal(str(row["sz"])),
                    side=Side.BUY if row.get("side") == "B" else Side.SELL,
                    ts=datetime.fromtimestamp(row["time"] / 1000, tz=timezone.utc),
                )
            )
        return out
