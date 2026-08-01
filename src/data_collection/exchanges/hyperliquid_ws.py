"""Hyperliquid public trade tape over websocket.

The first stream adapter, and the feed behind funding-settlement capture: with
the gate in scheduler/capture.py this is connected permanently but only writes
inside a window around each settlement — about 5% of the tape on an hourly
venue, and the 5% that carries the information.

Contract (verified against Hyperliquid's websocket docs, 2026-07-27):

    endpoint   wss://api.hyperliquid.xyz/ws
    subscribe  {"method":"subscribe","subscription":{"type":"trades","coin":"BTC"}}
               one frame per coin — there is no multi-coin form
    data       {"channel":"trades","data":[WsTrade, ...]}
    ack        {"channel":"subscriptionResponse", ...}
    WsTrade    coin, side ("B"/"A"), px, sz, time (ms), tid, hash, users

`tid` is documented as a 50-bit hash of (buyer_oid, seller_oid) and is what
makes writes idempotent: it lands in trades.trade_id, so a resubscribe replay
or an overlapping REST backfill dedups in the database for free.

No application-level keepalive: their docs specify none, and instead tell
automated clients to expect periodic unannounced disconnects and reconnect
gracefully — which is exactly what WsClient does. picows' protocol-level
auto-ping covers idle detection.

Deliberately NOT populating wallet_address
------------------------------------------
Every WsTrade carries `users` = [buyer, seller], so this feed knows both
counterparties to every fill — genuinely interesting for settlement capture,
since it would show *which* wallets do the funding dodge. But Trade has one
wallet_address field and a public trade has two sides, so writing either one
would be a claim we cannot support. Migration 001 already anticipates this
("the WS phase may split it into maker/taker addresses"); until that split
exists, the tape stores no attribution. The per-account fills feed is the
path that legitimately fills wallet_address.
"""

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
        # orjson takes bytes directly — no intermediate decode. This runs in
        # picows' sync callback, so it must stay allocation-light.
        message = orjson.loads(payload)
        if message.get("channel") != "trades":
            return ()                     # subscriptionResponse, pong, errors

        data = message.get("data") or []
        if isinstance(data, dict):        # tolerate a single-object form
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
                    # Same derivation the REST adapter uses: spot coins are
                    # "X/USDC" or "@{index}", everything else is a perp.
                    market_type=HyperliquidScraper.market_type_for(coin),
                    symbol=coin,
                    trade_id=str(tid),
                    price=Decimal(str(row["px"])),
                    amount=Decimal(str(row["sz"])),
                    # "B" is a buy; anything else is the ask side. Matches
                    # HyperliquidScraper.fetch_fills so REST and WS rows agree.
                    side=Side.BUY if row.get("side") == "B" else Side.SELL,
                    ts=datetime.fromtimestamp(row["time"] / 1000, tz=timezone.utc),
                )
            )
        return out
