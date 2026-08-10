from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import ClassVar

import orjson

from core.enums import Exchange, MarketType, Side
from core.models import Trade
from data_collection.base import Capability
from data_collection.exchanges.bybit import to_canonical
from data_collection.streams import BaseExchangeStream


class BybitPerpTradesStream(BaseExchangeStream):
    exchange: ClassVar[Exchange] = Exchange.BYBIT
    ws_url: ClassVar[str] = "wss://stream.bybit.com/v5/public/linear"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.TRADES})
    kind: ClassVar[str] = "trades"

    keepalive_seconds: ClassVar[float | None] = 15.0

    def keepalive_frames(self) -> Sequence[bytes]:
        return [orjson.dumps({"op": "ping"})]

    def subscribe_frames(self) -> Sequence[bytes]:
        args = [f"publicTrade.{s.replace('/', '').upper()}" for s in self.symbols]
        return [orjson.dumps({"op": "subscribe", "args": args})]

    def parse(self, payload: bytes) -> Sequence[Trade]:
        message = orjson.loads(payload)
        topic = message.get("topic") or ""
        if not topic.startswith("publicTrade."):
            return ()            

        out: list[Trade] = []
        for row in message.get("data") or []:
            native = row.get("s")
            trade_id = row.get("i")
            if native is None or trade_id is None:
                continue
            out.append(
                Trade(
                    exchange=self.exchange,
                    market_type=MarketType.PERP,
                    symbol=to_canonical(native),
                    trade_id=str(trade_id),
                    price=Decimal(str(row["p"])),
                    amount=Decimal(str(row["v"])),
                    # Already the aggressor's side — no inversion.
                    side=Side.BUY if row.get("S") == "Buy" else Side.SELL,
                    ts=datetime.fromtimestamp(row["T"] / 1000, tz=timezone.utc),
                )
            )
        return out
