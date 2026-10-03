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
from data_collection.exchanges.hyperliquid import HyperliquidDeployerScraper, HyperliquidScraper
from data_collection.exchanges.lighter import LighterScraper
from data_collection.exchanges.risex import RiseScraper

REGISTRY: dict[str, type[BaseExchangeScraper]] = {
    "binance_spot": BinanceSpotScraper,
    "binance_perp": BinanceFuturesScraper,
    "bybit_spot": BybitSpotScraper,
    "bybit_perp": BybitPerpScraper,
    "hyperliquid": HyperliquidScraper,
    "hyperliquid_xyz": HyperliquidDeployerScraper,
    "hyperliquid_io": HyperliquidDeployerScraper,
    "lighter": LighterScraper,
    "extended": ExtendedScraper,
    "bulk": BulkScraper,          
    "rise": RiseScraper,          
    "bullet": BulletScraper,      
    "hylo": HyloScraper,
}

STREAM_REGISTRY: dict[str, type[BaseExchangeStream]] = {
    "hyperliquid": HyperliquidTradesStream,
    "bybit_perp": BybitPerpTradesStream,
    "binance_perp": BinanceFuturesTradesStream,
}