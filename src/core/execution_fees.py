"""Public perpetual taker fees, in basis points; None means not safely known.

Static estimates exclude funding, liquidation, builder/integrator charges and
account discounts. Rates are strings to preserve decimal arithmetic in callers.
`label` names the product, shown as a tooltip; venue names come from
`core.symbols.VENUE_NAMES`.

A tier's `taker_bps` applies to every market unless `class_taker_bps` gives an
asset class (commodity, index, equity) its own rate, as venues with a separate
traditional-asset schedule do. `funding_spread`, where set, is the share of
every funding payment the venue keeps from each side: the payer pays
(1 + spread) times the payment and the receiver gets (1 - spread) times it.
"""

TRADITIONAL_CLASSES = ("commodity", "index", "equity")


def _traditional(bps: str) -> dict[str, str]:
    """One rate for every traditional asset class."""
    return dict.fromkeys(TRADITIONAL_CLASSES, bps)


FEE_SCHEDULES = {
    "binance_perp": {
        "label": "Binance USD-M (USDT)",
        "default_tier": "vip_0",
        "source_url": "https://www.binance.com/en/fee/futureFee",
        "checked_at": "2026-10-12",
        "notes": [
            "Standard USDT-margined perpetual taker column for crypto; not spot, COIN-M or USDC promotions.",
            "TradFi perpetuals (commodities and equities) pay Binance's TradFi rate: 20% off the standard taker rate for Regular to VIP 3 and 50% off for VIP 4 to 9, a promotion running from 2026-03-31 until further notice (https://www.binance.com/en/support/announcement/detail/e3424d3a781841eb8b507bc694c0ca3a, checked 2026-11-17).",
            "No BNB fee-payment discount, referral discount or other promotion assumed. BNB balances below are VIP qualification requirements, not fee-payment discounts.",
            "Labels show the 30-day futures-volume AND BNB-balance qualification route. Other Binance VIP programs can also qualify; USD-equivalent volume includes USD-M and COIN-M futures.",
        ],
        "tiers": [
            {"id": "vip_0", "label": "Regular user (VIP 0)", "taker_bps": "5.0", "class_taker_bps": _traditional("4.0")},
            {"id": "vip_1", "label": "VIP 1 (30d $5M+ and 5+ BNB)", "taker_bps": "5.0", "class_taker_bps": _traditional("4.0")},
            {"id": "vip_2", "label": "VIP 2 (30d $10M+ and 25+ BNB)", "taker_bps": "4.0", "class_taker_bps": _traditional("3.2")},
            {"id": "vip_3", "label": "VIP 3 (30d $50M+ and 100+ BNB)", "taker_bps": "3.2", "class_taker_bps": _traditional("2.56")},
            {"id": "vip_4", "label": "VIP 4 (30d $600M+ and 500+ BNB)", "taker_bps": "3.0", "class_taker_bps": _traditional("1.5")},
            {"id": "vip_5", "label": "VIP 5 (30d $1B+ and 1,000+ BNB)", "taker_bps": "2.7", "class_taker_bps": _traditional("1.35")},
            {"id": "vip_6", "label": "VIP 6 (30d $2.5B+ and 1,750+ BNB)", "taker_bps": "2.5", "class_taker_bps": _traditional("1.25")},
            {"id": "vip_7", "label": "VIP 7 (30d $5B+ and 3,000+ BNB)", "taker_bps": "2.2", "class_taker_bps": _traditional("1.1")},
            {"id": "vip_8", "label": "VIP 8 (30d $12.5B+ and 4,500+ BNB)", "taker_bps": "2.0", "class_taker_bps": _traditional("1.0")},
            {"id": "vip_9", "label": "VIP 9 (30d $25B+ and 5,500+ BNB)", "taker_bps": "1.7", "class_taker_bps": _traditional("0.85")},
        ],
    },
    "bybit_perp": {
        "label": "Bybit USDT perpetuals",
        "default_tier": "vip_0",
        "source_url": "https://www.bybit.com/en/help-center/article/Trading-Fee-Structure",
        "checked_at": "2026-10-12",
        "notes": [
            "Published base perpetual/futures rates for crypto, not spot, Pre-Market or Innovation Zone rates. Regional and account-specific rates may differ; Bybit directs users to My Fee Rate after identity verification.",
            "TradFi perpetuals (US single-name equities, index ETFs and commodities such as gold, silver and crude oil) pay Bybit's dedicated TradFi schedule from 2026-06-16 (https://announcements.bybit.com/en/article/tradfi-perpetuals-lower-fees-across-all-tiers-bltb196506dada4be39/, checked 2026-11-17).",
            "No MNT, referral, promotional or negotiated Pro/market-maker discount assumed.",
            "Labels show the 30-day derivatives-volume qualification route; balance, borrowing and other product-volume routes also exist. VIP 4, VIP 5 and Supreme volume qualification requires API trading volume of at most 20%. VIP refreshes daily at 07:00 UTC; subaccounts inherit their main account's tier.",
        ],
        "tiers": [
            {"id": "vip_0", "label": "VIP 0 (non-VIP)", "taker_bps": "5.5", "class_taker_bps": _traditional("2.75")},
            {"id": "vip_1", "label": "VIP 1 (30d derivatives $10M+)", "taker_bps": "4.0", "class_taker_bps": _traditional("2.0")},
            {"id": "vip_2", "label": "VIP 2 (30d derivatives $25M+)", "taker_bps": "3.75", "class_taker_bps": _traditional("1.87")},
            {"id": "vip_3", "label": "VIP 3 (30d derivatives $50M+)", "taker_bps": "3.5", "class_taker_bps": _traditional("1.75")},
            {"id": "vip_4", "label": "VIP 4 (30d derivatives $100M+, API at most 20%)", "taker_bps": "3.2", "class_taker_bps": _traditional("1.6")},
            {"id": "vip_5", "label": "VIP 5 (30d derivatives $250M+, API at most 20%)", "taker_bps": "3.2", "class_taker_bps": _traditional("1.6")},
            {"id": "supreme_vip", "label": "Supreme VIP (30d derivatives $500M+, API at most 20%)", "taker_bps": "3.0", "class_taker_bps": _traditional("1.5")},
        ],
    },
    "hyperliquid": {
        "label": "Hyperliquid native perpetuals",
        "default_tier": "0",
        "source_url": "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees",
        "checked_at": "2026-10-12",
        "notes": [
            "Validator-operated perpetual base rates only; HIP-3 deployer markets use additional market-specific multipliers and must not inherit these all-in rates.",
            "14-day weighted volume = perpetual volume + 2 * spot volume; evaluated daily in UTC. Subaccount volume aggregates with the master account; vault volume is separate.",
            "No HYPE staking or referral discount, aligned-quote discount, growth-mode discount or builder charge assumed. Aligned quote assets can reduce protocol taker fees by 20%.",
        ],
        "tiers": [
            {"id": "0", "label": "Tier 0 (base)", "taker_bps": "4.5"},
            {"id": "1", "label": "Tier 1 (14d weighted volume over $5M)", "taker_bps": "4.0"},
            {"id": "2", "label": "Tier 2 (14d weighted volume over $25M)", "taker_bps": "3.5"},
            {"id": "3", "label": "Tier 3 (14d weighted volume over $100M)", "taker_bps": "3.0"},
            {"id": "4", "label": "Tier 4 (14d weighted volume over $500M)", "taker_bps": "2.8"},
            {"id": "5", "label": "Tier 5 (14d weighted volume over $2B)", "taker_bps": "2.6"},
            {"id": "6", "label": "Tier 6 (14d weighted volume over $7B)", "taker_bps": "2.4"},
        ],
    },
    "hyperliquid_xyz": {
        "label": "Hyperliquid XYZ (HIP-3)",
        "default_tier": "market_specific",
        "source_url": "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees",
        "checked_at": "2026-10-12",
        "notes": [
            "No universal XYZ all-in taker rate is asserted. The account shares native Hyperliquid volume tiers, but each HIP-3 market's deployer fee scale, growth mode and quote asset must be resolved before pricing its fee.",
            "Official developer formula: for deployerFeeScale s, HIP-3 multiplier is 1+s when s < 1, otherwise 2*s; growth mode additionally multiplies by 0.1. Aligned quote discounts apply to the protocol share, not blindly to the entire fee. Staking/referral discounts are not assumed.",
            "None means the effective market-specific fee is unknown, not zero. Do not substitute the native perpetual base rate or a fixed 2x multiplier for every XYZ market.",
        ],
        "tiers": [
            {"id": "market_specific", "label": "Market-specific HIP-3 fee (unresolved)", "taker_bps": None},
        ],
    },
    "lighter": {
        "label": "Lighter",
        "default_tier": "standard",
        "source_url": "https://docs.lighter.xyz/trading/trading-fees",
        "checked_at": "2026-10-12",
        "notes": [
            "Standard is the documented default account type with zero trading fees across perpetual and spot markets, not a maker rebate. Plus and Premium are opt-in account types, not volume VIP levels.",
            "One schedule for crypto and traditional-asset perpetuals alike (checked 2026-11-17).",
            "Premium rate is the published 0-LIT-staked base rate; no LIT staking discount or fee credits assumed. The official staking schedule publishes lower Premium rates, but they are excluded from this undiscounted catalog.",
            "Standard and Plus taker latency is 300 ms; Premium is 140 ms. Account types have different API quotas and maker/cancel latencies; this snapshot estimator does not model those delays.",
        ],
        "tiers": [
            {"id": "standard", "label": "Standard (default account)", "taker_bps": "0"},
            {"id": "premium", "label": "Premium (opt-in; 0 LIT staked)", "taker_bps": "2.8"},
            {"id": "plus", "label": "Plus (opt-in; higher rate limits)", "taker_bps": "0.5"},
        ],
    },
    "extended": {
        "label": "Extended",
        "default_tier": "base",
        "source_url": "https://docs.extended.exchange/extended-resources/trading/trading-fees-and-rebates",
        "checked_at": "2026-10-12",
        "notes": [
            "Flat regular perpetual/spot taker rate; no published taker VIP tiers. Maker-share rebates are not taker discounts.",
            "RWA markets (commodities, indices and equities) trade at a published discount to the regular rate, 0.010% taker (1.0 bps), with no end date; Extended reserves the right to change it (checked 2026-11-17).",
            "Builder/integrator fees, if applicable, are additional and excluded.",
        ],
        "tiers": [
            {"id": "base", "label": "Regular market (flat base fee)", "taker_bps": "2.5", "class_taker_bps": _traditional("1.0")},
        ],
    },
    "rise": {
        "label": "RISEx",
        "default_tier": "1",
        "source_url": "https://docs.risechain.com/docs/risex/trading/fees",
        "checked_at": "2026-10-27",
        "notes": [
            "Published rolling 14-day volume tiers; the taker rate is used. RISEx has no maker rebate program.",
            "The schedule does not name its traditional-asset markets and publishes no separate rate for them, so it is applied to them unchanged (checked 2026-11-17).",
            "Market makers can be offered trial tiers, and builder fees may add to actual charges. The authenticated GET /v1/user/fees lookup (JWT, caller-scoped) is not used, so your own tier is chosen here, not read.",
        ],
        "tiers": [
            {"id": "1", "label": "Tier 1 (14d volume under $5M)", "taker_bps": "3.0"},
            {"id": "2", "label": "Tier 2 (14d volume $5M+)", "taker_bps": "2.5"},
            {"id": "3", "label": "Tier 3 (14d volume $25M+)", "taker_bps": "2.1"},
            {"id": "4", "label": "Tier 4 (14d volume $100M+)", "taker_bps": "1.7"},
            {"id": "5", "label": "Tier 5 (14d volume $500M+)", "taker_bps": "1.55"},
            {"id": "6", "label": "Tier 6 (14d volume $1B+)", "taker_bps": "1.5"},
        ],
    },
    "bullet": {
        "label": "Bullet",
        "default_tier": "base",
        "source_url": "https://docs.bullet.xyz/exchange/trading/fees",
        "checked_at": "2026-11-17",
        "funding_spread": "0.05",
        "notes": [
            "Zero maker and taker fees on every market; no volume tiers.",
            "Bullet keeps 5% of every hourly funding payment from each side: the payer pays 105% of it and the receiver gets 95%. Holds include this spread; published funding rates are gross.",
        ],
        "tiers": [
            {"id": "base", "label": "All accounts (no volume tiers)", "taker_bps": "0"},
        ],
    },
    "bulk": {
        "label": "BULK",
        "default_tier": "1",
        "source_url": "https://docs.bulk.trade/bulk-exchange/fees",
        "checked_at": "2026-10-12",
        "notes": [
            "Published taker tiers are identical in Genesis Liquidity Phase 1 and Public Fee Phase 2; only maker rates change. No maker rebates, Alpha rewards, referral or builder discounts assumed.",
            "Verified active mainnet policy at https://mainnet-api1.bulk.trade/api/v1/feeState: 14-day window, the listed taker rates and no scheduled changes at verification time. Future policy/instrument overrides can change these static rates.",
            "TradFi markets pay one flat 1.5 bps taker rate at every volume while BULK is in Growth Mode, which has no published end; feeState lists it as an override on each TradFi instrument (checked 2026-11-17).",
            "Qualification uses trailing 14 completed UTC days of executed USD notional, aggregated across the main account and subaccounts; thresholds are inclusive. Current-day volume affects tiers only after the next 00:00 UTC cutoff.",
        ],
        "tiers": [
            {"id": "1", "label": "Tier 1 (14d volume under $1M)", "taker_bps": "3.5", "class_taker_bps": _traditional("1.5")},
            {"id": "2", "label": "Tier 2 (14d volume $1M+)", "taker_bps": "3.3", "class_taker_bps": _traditional("1.5")},
            {"id": "3", "label": "Tier 3 (14d volume $10M+)", "taker_bps": "3.0", "class_taker_bps": _traditional("1.5")},
            {"id": "4", "label": "Tier 4 (14d volume $50M+)", "taker_bps": "2.8", "class_taker_bps": _traditional("1.5")},
            {"id": "5", "label": "Tier 5 (14d volume $150M+)", "taker_bps": "2.6", "class_taker_bps": _traditional("1.5")},
            {"id": "6", "label": "Tier 6 (14d volume $500M+)", "taker_bps": "2.4", "class_taker_bps": _traditional("1.5")},
            {"id": "7", "label": "Tier 7 (14d volume $1.5B+)", "taker_bps": "2.3", "class_taker_bps": _traditional("1.5")},
            {"id": "8", "label": "Tier 8 (14d volume $4B+)", "taker_bps": "2.2", "class_taker_bps": _traditional("1.5")},
        ],
    },
    "phoenix": {
        "label": "Phoenix",
        "default_tier": "base",
        "source_url": "https://docs.phoenix.trade/phoenix/matching-engine/fees",
        "checked_at": "2026-11-17",
        "notes": [
            "One published taker rate for crypto, commodity and equity perpetuals, matching the takerFee its markets API publishes per market; FX perpetuals pay a tenth of it. No volume tiers.",
            "No referral discount assumed; Flight builder fees add to the exchange fee and are excluded.",
        ],
        "tiers": [
            {"id": "base", "label": "All accounts (no volume tiers)", "taker_bps": "3.5"},
        ],
    },
    "qfex": {
        "label": "QFEX",
        "default_tier": "tier_5",
        "source_url": "https://docs.qfex.com/qfex/fees",
        "checked_at": "2026-11-17",
        "notes": [
            "Taker rates by asset class, on trailing 30-day effective volume = 2 × commodities + 2 × indices + 5 × single-stock volume. QFEX lists no crypto.",
            "Accounts that had deposited by 24 August 2026 pay the lowest tier for life on listed products, including US100, NVDA, MU, CRCL, META, GOOGL, AAPL, MSFT and MSTR (1.5 bps equities, 0.6 bps indices); choose Tier 1 to price those.",
            "Referral rewards and builder-code charges are excluded.",
        ],
        "tiers": [
            {"id": "tier_5", "label": "Tier 5 (30d effective volume under $2M)", "taker_bps": None,
             "class_taker_bps": {"commodity": "5.0", "index": "5.0", "equity": "10.0"}},
            {"id": "tier_4", "label": "Tier 4 (30d effective volume $2M+)", "taker_bps": None,
             "class_taker_bps": {"commodity": "4.0", "index": "4.0", "equity": "8.0"}},
            {"id": "tier_3", "label": "Tier 3 (30d effective volume $10M+)", "taker_bps": None,
             "class_taker_bps": {"commodity": "2.0", "index": "2.0", "equity": "5.0"}},
            {"id": "tier_2", "label": "Tier 2 (30d effective volume $100M+)", "taker_bps": None,
             "class_taker_bps": {"commodity": "1.0", "index": "1.0", "equity": "2.0"}},
            {"id": "tier_1", "label": "Tier 1 (30d effective volume $400M+)", "taker_bps": None,
             "class_taker_bps": {"commodity": "0.6", "index": "0.6", "equity": "1.5"}},
        ],
    },
}


def execution_listings(registry, asset: str) -> dict[str, str]:
    """Venue -> native symbol for every compared venue listing the asset.

    A venue is compared when it has a fee schedule; order follows the schedules.
    """
    available = registry.listings(asset)
    return {venue: available[venue] for venue in FEE_SCHEDULES if venue in available}


def selected_tiers(requested: dict[str, str]) -> dict[str, dict]:
    """Each venue's chosen tier, defaulting to its base tier.

    Raises ValueError for unknown venues or tiers.
    """
    if requested.keys() - FEE_SCHEDULES.keys():
        raise ValueError("Fee selections must use supported venue IDs.")
    selected = {}
    for venue, schedule in FEE_SCHEDULES.items():
        tier_id = requested.get(venue, schedule["default_tier"])
        tier = next((tier for tier in schedule["tiers"] if tier["id"] == tier_id), None)
        if tier is None:
            raise ValueError(f"Unknown fee tier for {schedule['label']}.")
        selected[venue] = tier
    return selected


def taker_bps(tier: dict, asset_class: str) -> str | None:
    """A tier's taker rate for one asset class: the class's own rate where the
    venue publishes one, otherwise the tier's rate for every market."""
    rates = tier.get("class_taker_bps", {})
    return rates[asset_class] if asset_class in rates else tier["taker_bps"]


def funding_spread(venue: str) -> str:
    """The share of each funding payment the venue keeps from either side."""
    return FEE_SCHEDULES.get(venue, {}).get("funding_spread", "0")
