from __future__ import annotations

from data_collection.base import BaseExchangeScraper
from data_collection.exchanges.binance_ws import BinanceFuturesTradesStream
from data_collection.exchanges.bybit_ws import BybitPerpTradesStream
from data_collection.exchanges.hyperliquid_ws import HyperliquidTradesStream
from data_collection.streams import BaseExchangeStream
from data_collection.exchanges.binance import BinanceFuturesScraper, BinanceSpotScraper
from data_collection.exchanges.bulk import BulkScraper
from data_collection.exchanges.bullet import BulletScraper
from data_collection.exchanges.bybit import BybitPerpScraper, BybitSpotScraper
from data_collection.exchanges.extended import ExtendedScraper
from data_collection.exchanges.hylo import HyloScraper
from data_collection.exchanges.hyperliquid import HyperliquidScraper
from data_collection.exchanges.lighter import LighterScraper
from data_collection.exchanges.risex import RiseScraper

REGISTRY: dict[str, type[BaseExchangeScraper]] = {
    "binance_spot": BinanceSpotScraper,
    "binance_perp": BinanceFuturesScraper,
    "bybit_spot": BybitSpotScraper,
    "bybit_perp": BybitPerpScraper,
    "hyperliquid": HyperliquidScraper,
    "lighter": LighterScraper,
    "extended": ExtendedScraper,
    "bulk": BulkScraper,          # TESTNET adapter — enable config listings at mainnet
    "rise": RiseScraper,          # TESTNET adapter — enable config listings at mainnet
    "bullet": BulletScraper,      # mainnet LIVE — run the curl checklist in bullet.py, then enable
    # Leveraged tokens, not an order-book venue. Registered but intentionally
    # absent from symbols.toml: its API is behind an anti-bot challenge, so no
    # targets are built and nothing polls. See the checklist in hylo.py.
    "hylo": HyloScraper,
}

# Websocket adapters, keyed by the same venue ids. Separate from REGISTRY
# because the two are wired up by different machinery — APScheduler polls
# scrapers, scheduler/streams.py supervises streams — and most venues will
# have one without the other for a while.
STREAM_REGISTRY: dict[str, type[BaseExchangeStream]] = {
    "hyperliquid": HyperliquidTradesStream,
    "bybit_perp": BybitPerpTradesStream,
    # Written and parse-verified, but fstream.binance.com delivered no market
    # data from the dev machine on 2026-07-27 (spot streams worked, REST fapi
    # worked) — looks like a jurisdiction restriction on the futures socket.
    # Smoke-test it from the soak server before enabling in symbols.toml.
    "binance_perp": BinanceFuturesTradesStream,
}