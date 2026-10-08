from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class SymbolConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AssetMetadata:
    family: str
    asset_class: str
    group: str
    label: str


@dataclass(frozen=True, slots=True)
class VenueIdentity:
    key: str
    label: str
    deployer: str | None = None


@dataclass(frozen=True)
class SymbolRegistry:
    # asset id -> {venue id -> venue-native symbol}
    _by_asset: dict[str, dict[str, str]] = field(default_factory=dict)
    # venue-native symbol -> asset id (venue-agnostic; conflicts rejected)
    _by_symbol: dict[str, str] = field(default_factory=dict)
    _metadata: dict[str, AssetMetadata] = field(default_factory=dict)
    _hyperliquid_deployers: dict[str, str] = field(default_factory=dict)

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_config(cls, data: dict) -> "SymbolRegistry":
        """Build symbol, taxonomy, and deployer identity from symbols.toml."""
        assets_cfg = data.get("assets", {})
        by_asset: dict[str, dict[str, str]] = {}
        by_symbol: dict[str, str] = {}
        metadata: dict[str, AssetMetadata] = {}
        deployer_labels: dict[str, str] = {}
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

        metadata_cfg = data.get("asset_metadata", {})
        if not isinstance(metadata_cfg, dict):
            errors.append("[asset_metadata] must contain asset tables")
        else:
            allowed_families = {"crypto", "traditional"}
            allowed_classes = {"crypto", "commodity", "index", "equity"}
            for asset, entry in metadata_cfg.items():
                if asset not in by_asset:
                    errors.append(f"[asset_metadata.{asset}] has no matching [assets.{asset}]")
                    continue
                if not isinstance(entry, dict):
                    errors.append(f"[asset_metadata.{asset}] must be a table")
                    continue
                family = entry.get("family")
                asset_class = entry.get("asset_class")
                group = entry.get("group")
                label = entry.get("label")
                if family not in allowed_families:
                    errors.append(
                        f"[asset_metadata.{asset}] invalid family {family!r}"
                    )
                if asset_class not in allowed_classes:
                    errors.append(
                        f"[asset_metadata.{asset}] invalid asset_class {asset_class!r}"
                    )
                for name, value in (("group", group), ("label", label)):
                    if not isinstance(value, str) or not value:
                        errors.append(
                            f"[asset_metadata.{asset}] invalid {name} {value!r}"
                        )
                if (
                    family in allowed_families
                    and asset_class in allowed_classes
                    and isinstance(group, str) and group
                    and isinstance(label, str) and label
                ):
                    metadata[asset] = AssetMetadata(
                        family=family,
                        asset_class=asset_class,
                        group=group,
                        label=label,
                    )

        deployers_cfg = data.get("hyperliquid_deployers", {})
        if not isinstance(deployers_cfg, dict):
            errors.append("[hyperliquid_deployers] must contain deployer tables")
        else:
            for deployer, entry in deployers_cfg.items():
                label = entry.get("label") if isinstance(entry, dict) else None
                if not isinstance(label, str) or not label:
                    errors.append(
                        f"[hyperliquid_deployers.{deployer}] invalid label {label!r}"
                    )
                else:
                    deployer_labels[deployer] = label

        if errors:
            raise SymbolConfigError(
                "invalid symbols config:\n  - " + "\n  - ".join(errors)
            )
        return cls(by_asset, by_symbol, metadata, deployer_labels)

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

    def metadata(self, asset: str) -> AssetMetadata:
        return self._metadata.get(
            asset,
            AssetMetadata(
                family="crypto",
                asset_class="crypto",
                group="crypto",
                label=asset,
            ),
        )

    def _hip3_label(self, deployer: str) -> str:
        return f"hip3:{self._hyperliquid_deployers.get(deployer, deployer).lower()}"

    def hip3_venue_labels(self) -> dict[str, str]:
        """Configured HIP-3 display names, keyed by stable venue identity."""
        return {
            f"hyperliquid:{deployer}": self._hip3_label(deployer)
            for deployer in self._hyperliquid_deployers
        }

    def venue_identity(self, exchange: str, symbol: str) -> VenueIdentity:
        if exchange == "hyperliquid" and ":" in symbol:
            deployer = symbol.split(":", 1)[0]
            return VenueIdentity(
                key=f"hyperliquid:{deployer}",
                label=self._hip3_label(deployer),
                deployer=deployer,
            )
        return VenueIdentity(key=exchange, label=exchange)

    def venue_symbols(self, venue: str) -> list[str]:
        """Every symbol this config lists on one venue."""
        return [
            listings[venue]
            for listings in self._by_asset.values()
            if venue in listings
        ]