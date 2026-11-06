from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1/overseer"
        )
    )
   
    discord_webhook_url: str | None = field(
        default_factory=lambda: os.getenv("DISCORD_WEBHOOK_URL") or None
    )
  
    symbols_file: str = field(
        default_factory=lambda: os.getenv("OVERSEER_SYMBOLS_FILE", "symbols.toml")
    )

    flask_secret_key: str = field(
        default_factory=lambda: os.getenv("FLASK_SECRET_KEY", "")
    )

    # Discord ping when a coin's cross-venue funding spread exceeds this (% APR).
    spread_alert_apr: float = field(
        default_factory=lambda: float(os.getenv("OVERSEER_SPREAD_ALERT_APR", "25"))
    )

    # Spread alerts ignore markets with less open interest than this (USD).
    spread_alert_min_oi_usd: float = field(
        default_factory=lambda: float(
            os.getenv("OVERSEER_SPREAD_ALERT_MIN_OI", "1000000")
        )
    )

    # A spread must persist across fresh snapshots for this long before alerting.
    spread_confirm_minutes: float = field(
        default_factory=lambda: float(
            os.getenv("OVERSEER_SPREAD_CONFIRM_MINUTES", "10")
        )
    )

    # Discord ping when a venue's TVL falls this far (%) below its high of the
    # trailing window (hours); a fast fall can mean an exploit or a run.
    tvl_drop_pct: float = field(
        default_factory=lambda: float(os.getenv("OVERSEER_TVL_DROP_PCT", "10"))
    )
    tvl_drop_hours: float = field(
        default_factory=lambda: float(os.getenv("OVERSEER_TVL_DROP_HOURS", "6"))
    )


settings = Settings()