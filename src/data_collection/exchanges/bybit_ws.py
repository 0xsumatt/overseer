"""Bybit v5 linear-perp trade tape over websocket.

The second settlement-capture venue, and the first to use the application-level
keepalive hook: Bybit closes an idle socket unless the client sends
{"op":"ping"} roughly every 20s. The websocket protocol's own ping/pong does
not satisfy it, which is exactly the case BaseExchangeStream.keepalive_frames
exists for.

Contract (verified live 2026-07-27):

    endpoint   wss://stream.bybit.com/v5/public/linear
    subscribe  {"op":"subscribe","args":["publicTrade.BTCUSDT", ...]}
               one frame covers every symbol
    ack        {"success":true,"ret_msg":"","conn_id":"...","op":"subscribe"}
    data       {"topic":"publicTrade.BTCUSDT","type":"snapshot","ts":<ms>,
                "data":[{"T":<trade ms>,"s":"BTCUSDT","S":"Buy"|"Sell",
                         "v":"<qty>","p":"<price>","L":"<tick dir>",
                         "i":"<trade id>","BT":false}]}

Nicer than Binance in two ways worth noting, because they remove failure modes
rather than just being conveniences:

* `S` is the TAKER side directly ("Buy"/"Sell"), so there is no maker/taker
  inversion to get backwards — contrast the `m` flag in binance_ws.py.
* `i` is a real trade id (a uuid), unique per fill, so it maps straight onto
  trades.trade_id and dedups a reconnect replay without any synthesis.

`type` is "snapshot" on these messages even mid-stream; it is not a
book-style snapshot/delta distinction and needs no special handling here.
"""

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

    # Bybit's documented idle timeout is 20s; ping at 15s for margin.
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
            return ()              # subscribe ack, pong, errors

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
