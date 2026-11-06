"""Venue TVL from DefiLlama. `/tvl/{slug}` answers a bare USD number that
DefiLlama's adapters update about once a day; the API caches it for an hour."""

from __future__ import annotations

from decimal import Decimal

from core.enums import Exchange
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter

# What each venue's TVL measures. Exchange reserves (Binance, Bybit) and Hylo's
# SOL collateral move with prices; the perp DEXs hold mostly USDC, so a fall
# there is money leaving. DefiLlama lists only volume for Bullet and QFEX.
DEFILLAMA_TVL: dict[Exchange, str] = {
    Exchange.BINANCE: "binance-cex",
    Exchange.BYBIT: "bybit",
    Exchange.HYPERLIQUID: "hyperliquid-bridge",  # deposits, HIP-3 markets included
    Exchange.LIGHTER: "lighter-bridge",
    Exchange.EXTENDED: "extended-perps",
    Exchange.BULK: "bulk",
    Exchange.RISE: "risex",
    Exchange.HYLO: "hylo-protocol",
    Exchange.PHOENIX: "phoenix-perp",
}


class DefiLlamaTvl:
    base_url = "https://api.llama.fi"

    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient(
            limiter=RateLimiter.per_minute(60, burst=10),
            default_headers={"User-Agent": "overseer/0.1"},
        )

    async def fetch(self, slug: str) -> Decimal | None:
        """Current TVL in USD, or None where DefiLlama publishes none."""
        response = await self.http.request("get", f"{self.base_url}/tvl/{slug}")
        text = (await response.text()).strip()
        return Decimal(text) if text else None

    async def aclose(self) -> None:
        await self.http.aclose()
