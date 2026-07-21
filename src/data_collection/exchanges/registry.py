from __future__ import annotations

from data_collection.base import BaseExchangeScraper
from data_collection.exchanges.binance import BinanceFuturesScraper, BinanceSpotScraper
from data_collection.exchanges.bulk import BulkScraper
from data_collection.exchanges.bullet import BulletScraper
from data_collection.exchanges.bybit import BybitPerpScraper, BybitSpotScraper
from data_collection.exchanges.extended import ExtendedScraper
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
}