"""Web app factory.

    flask --app web:create_app run --debug            # dev
    gunicorn "web:create_app()" -w 2 -b 0.0.0.0:8000  # prod (behind a reverse proxy)

This process only READS the database (storage.queries) plus the tiny users
table. It never imports data_collection and never runs the scheduler — that
stays its own process, as ever.

User accounts are CLI-only (no self-registration):

    flask --app web:create_app create-user you@example.com --internal
"""

from __future__ import annotations

import os
import secrets
import tomllib

import click
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from core.config import settings
from core.symbols import SymbolRegistry
from storage.queries import ReadStorage
from web import auth as auth_module
from web.cache import ResponseCache
from web.views import api, pages


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _proxy_hops() -> int:
    try:
        hops = int(os.getenv("OVERSEER_PROXY_HOPS", "0"))
    except ValueError as exc:
        raise ValueError("OVERSEER_PROXY_HOPS must be an integer") from exc
    if hops < 0:
        raise ValueError("OVERSEER_PROXY_HOPS must not be negative")
    return hops


def create_app(database_url: str | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    proxy_hops = _proxy_hops()
    if proxy_hops:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=proxy_hops,
            x_proto=proxy_hops,
        )
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env_bool("OVERSEER_SECURE_COOKIES"),
    )

    secret = getattr(settings, "flask_secret_key", "") or os.getenv("FLASK_SECRET_KEY", "")
    if not secret:
        # Dev fallback: random per boot (sessions reset on restart). Set
        # FLASK_SECRET_KEY in .env for anything beyond local development.
        secret = secrets.token_hex(32)
        app.logger.warning("FLASK_SECRET_KEY not set — using a per-boot random key")
    app.secret_key = secret

    mock = os.getenv("OVERSEER_MOCK", "").lower() in ("1", "true", "yes")
    app.config["MOCK"] = mock
    if mock:
        # UI testing with no database: storage.mock serves every read query with
        # deterministic realistic data; log in as mock@overseer.local / mock.
        from storage.mock import MockStorage
        storage = MockStorage()
        app.extensions["symbols"] = SymbolRegistry.load(settings.symbols_file)
        app.logger.warning("MOCK MODE — serving generated data, no database")
        from storage.mock import MOCK_FILLS
        app.extensions["tracked_wallets"] = [
            {"venue": "hyperliquid" if a.startswith("0x") else "lighter",
             "address": a, "label": lbl}
            for a, lbl in MOCK_FILLS.items()
        ]
    else:
        storage = ReadStorage(database_url or settings.database_url)
        app.extensions["symbols"] = SymbolRegistry.load(settings.symbols_file)
        # tracked wallets ([[fills]] in symbols.toml) — the wallets page and
        # fills-pulse labels come from here, not the database.
        with open(settings.symbols_file, "rb") as f:
            fills_cfg = tomllib.load(f).get("fills", [])
        app.extensions["tracked_wallets"] = [
            {"venue": e["venue"], "address": e["address"], "label": e["label"]}
            for e in fills_cfg
        ]
    app.extensions["fills_labels"] = {
        w["address"]: w["label"] for w in app.extensions["tracked_wallets"]
    }
    app.extensions["read_storage"] = storage
    app.extensions["response_cache"] = ResponseCache(ttl_seconds=30)

    auth_module.init_auth(app, storage)
    app.register_blueprint(pages.bp)
    app.register_blueprint(api.bp)

    @app.cli.command("create-user")
    @click.argument("email")
    @click.option("--internal", is_flag=True, help="Grant access to /health")
    @click.password_option()
    def create_user_cmd(email: str, internal: bool, password: str) -> None:
        """Create (or update) a web account. The only way accounts are made."""
        auth_module.create_user(storage, email, password, internal)
        click.echo(f"user {email} ready (internal={internal})")

    return app