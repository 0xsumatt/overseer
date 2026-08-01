"""Binance USDⓈ-M futures trade tape over websocket.

The CEX leg of the settlement-capture comparison. Pairs with the Hyperliquid
stream on the app's central question — how a venue behaves around funding — and
exercises a different corner of the schedule: Binance settles every 8h (4h for
some symbols, which is why `interval_hours` is stored per symbol), against
Hyperliquid's hourly, so the two venues' capture windows rarely coincide.

Contract:

    endpoint   wss://fstream.binance.com/ws
    subscribe  {"method":"SUBSCRIBE","params":["btcusdt@aggTrade",...],"id":1}
               one frame covers every symbol, unlike Hyperliquid's per-coin form
    ack        {"result":null,"id":1}
    data       {"e":"aggTrade","E":<event ms>,"a":<agg id>,"s":"BTCUSDT",
                "p":"<price>","q":"<qty>","f":<first id>,"l":<last id>,
                "T":<trade ms>,"m":<buyer is maker>}

Binance's docs redirected to a landing page when checked on 2026-07-27, so the
field shape above was verified empirically against the live stream rather than
quoted. If parsing starts failing, re-run the smoke test before assuming a code
fault — this is the file most likely to drift.

Why aggTrade rather than @trade
-------------------------------
aggTrade collapses fills from one taker order at one price into a single event.
That is the same information for flow purposes at a fraction of the row count,
and it is the conventional feed for microstructure work. `a` (the aggregate
trade id) is stable and unique per symbol, so it serves as trade_id and keeps
writes idempotent across a reconnect replay.

Two details that are easy to get backwards
------------------------------------------
* `m` is "was the BUYER the maker". So m=true means the aggressor — the side
  that crossed the spread, which is the side we record — was the SELLER.
  Getting this inverted would silently flip every buy/sell in the tape.
* `T` is the trade time; `E` is when Binance emitted the event. We store `T`,
  because the analysis is about when the market traded, not when we heard.
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
from data_collection.exchanges.binance import to_canonical
from data_collection.streams import BaseExchangeStream


class BinanceFuturesTradesStream(BaseExchangeStream):
    exchange: ClassVar[Exchange] = Exchange.BINANCE
    ws_url: ClassVar[str] = "wss://fstream.binance.com/ws"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.TRADES})
    kind: ClassVar[str] = "trades"

    # No application-level keepalive: Binance sends websocket PING frames and
    # expects a protocol PONG, which picows answers automatically
    # (enable_auto_pong defaults to True).
    keepalive_seconds: ClassVar[float | None] = None

    def subscribe_frames(self) -> Sequence[bytes]:
        # Stream names are lower-case native symbols: "BTC/USDT" -> "btcusdt".
        params = [f"{s.replace('/', '').lower()}@aggTrade" for s in self.symbols]
        return [orjson.dumps({"method": "SUBSCRIBE", "params": params, "id": 1})]

    def parse(self, payload: bytes) -> Sequence[Trade]:
        message = orjson.loads(payload)
        if message.get("e") != "aggTrade":
            return ()                      # {"result":null,"id":1} acks, errors

        native = message.get("s")
        agg_id = message.get("a")
        if native is None or agg_id is None:
            return ()
        return (
            Trade(
                exchange=self.exchange,
                market_type=MarketType.PERP,
                # Canonical form shared with the REST adapter, so these rows
                # join to funding/liquidity for the same market — and so the
                # capture gate can find the symbol's settlement schedule.
                symbol=to_canonical(native),
                trade_id=str(agg_id),
                price=Decimal(str(message["p"])),
                amount=Decimal(str(message["q"])),
                # m=true -> buyer was the maker -> the aggressor was the seller.
                side=Side.SELL if message.get("m") else Side.BUY,
                ts=datetime.fromtimestamp(message["T"] / 1000, tz=timezone.utc),
            ),
        )
