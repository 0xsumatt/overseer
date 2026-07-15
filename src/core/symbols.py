from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class SymbolConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SymbolRegistry:
    # asset id -> {venue id -> venue-native symbol}
    _by_asset: dict[str, dict[str, str]] = field(default_factory=dict)
    # venue-native symbol -> asset id (venue-agnostic; conflicts rejected)
    _by_symbol: dict[str, str] = field(default_factory=dict)

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_config(cls, data: dict) -> "SymbolRegistry":
        """Build from a parsed symbols.toml dict (the [assets] tables)."""
        assets_cfg = data.get("assets", {})
        by_asset: dict[str, dict[str, str]] = {}
        by_symbol: dict[str, str] = {}
        errors: list[str] = []

        for asset, listings in assets_cfg.items():
            if not isinstance(listings, dict) or not listings:
                errors.append(f"[assets.{asset}] must map venue -> symbol")
                continue
            clean: dict[str, str] = {}
            for venue, symbol in listings.items():
                if not isinstance(symbol, str) or not symbol:
                    errors.append(f"[assets.{asset}] {venue}: invalid symbol {symbol!r}")
                    continue
                prior = by_symbol.get(symbol)
                if prior is not None and prior != asset:
                    errors.append(
                        f"symbol {symbol!r} maps to both {prior!r} and {asset!r} — "
                        "one venue symbol must mean one asset"
                    )
                    continue
                clean[venue] = symbol
                by_symbol[symbol] = asset
            if clean:
                by_asset[asset] = clean

        if errors:
            raise SymbolConfigError(
                "invalid [assets] config:\n  - " + "\n  - ".join(errors)
            )
        return cls(by_asset, by_symbol)

    @classmethod
    def load(cls, path: str | Path) -> "SymbolRegistry":
        with Path(path).open("rb") as f:
            return cls.from_config(tomllib.load(f))

    # -- lookups ---------------------------------------------------------------

    def assets(self) -> list[str]:
        return sorted(self._by_asset)

    def listings(self, asset: str) -> dict[str, str]:
        """venue -> native symbol for one asset."""
        return dict(self._by_asset.get(asset, {}))

    def symbol(self, asset: str, venue: str) -> str | None:
        """The venue-native symbol for an asset, or None if not listed there."""
        return self._by_asset.get(asset, {}).get(venue)

    def asset_for(self, symbol: str) -> str | None:
        """Reverse: venue-native symbol -> asset id (None if unmapped)."""
        return self._by_symbol.get(symbol)

    def venue_symbols(self, venue: str) -> list[str]:
        """Every symbol this config lists on one venue."""
        return [
            listings[venue]
            for listings in self._by_asset.values()
            if venue in listings
        ]