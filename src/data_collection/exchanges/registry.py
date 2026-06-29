"""Registry: venue-id -> scraper class.

Keyed by a venue-id *string*, not the Exchange enum, because one exchange can
have several REST adapters — Binance has separate spot and USDT-M perp endpoints,
so "binance_spot" and "binance_perp" are distinct venues. Hyperliquid serves both
markets through one endpoint, so it's a single venue. The stored `exchange` field
is still BINANCE for both Binance venues; market_type distinguishes them.

The venue-id is the section name in symbols.toml.
"""

from __future__ import annotations

from data_collection.base import BaseExchangeScraper
from data_collection.exchanges.binance import BinanceFuturesScraper, BinanceSpotScraper
from data_collection.exchanges.hyperliquid import HyperliquidScraper

REGISTRY: dict[str, type[BaseExchangeScraper]] = {
    "binance_spot": BinanceSpotScraper,
    "binance_perp": BinanceFuturesScraper,
    "hyperliquid": HyperliquidScraper,
}