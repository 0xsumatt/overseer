"""Runtime settings, env-driven.

Kept dependency-free for now; swap to pydantic-settings later if you want
validation and typed env parsing.
"""

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


settings = Settings()