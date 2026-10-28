"""Web app factory.

    flask --app web:create_app run --debug            # dev
    gunicorn "web:create_app()" -w 2 -b 0.0.0.0:8000  # prod (behind a reverse proxy)

This process reads market data and serves anonymous, transient account reads for
the browser-local Positions tool. Wallet configuration and tokens never enter
the database. Persistent market-data collection stays in the scheduler process.

User accounts are CLI-only (no self-registration):

    flask --app web:create_app create-user you@example.com --internal
"""

from __future__ import annotations

import os
import secrets
import tomllib

import click
from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from core.config import settings
from core.symbols import SymbolRegistry
from data_collection.execution_reader import ExecutionReader
from storage.queries import ReadStorage
from web import auth as auth_module
from web.cache import ResponseCache
from web.position_reader import PositionReader
from web.views import api, execution, margin, pages, positions


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
    app.extensions["symbols"] = SymbolRegistry.load(settings.symbols_file)
    with open(settings.symbols_file, "rb") as f:
        symbols_cfg = tomllib.load(f)
    if mock:
        # UI testing with no database: storage.mock serves every read query with
        # deterministic realistic data; log in as mock@overseer.local / mock.
        from storage.mock import MockStorage
        storage = MockStorage()
        app.logger.warning("MOCK MODE — serving generated data, no database")
        from storage.mock import MOCK_FILLS
        app.extensions["tracked_wallets"] = [
            {"venue": "hyperliquid" if a.startswith("0x") else "lighter",
             "address": a, "label": lbl}
            for a, lbl in MOCK_FILLS.items()
        ]
    else:
        storage = ReadStorage(database_url or settings.database_url)
        # tracked wallets ([[fills]] in symbols.toml) — the wallets page and
        # fills-pulse labels come from here, not the database.
        app.extensions["tracked_wallets"] = [
            {"venue": e["venue"], "address": e["address"], "label": e["label"]}
            for e in symbols_cfg.get("fills", [])
        ]
    # Trade flow only exists where a venue streams trades (`stream` in
    # symbols.toml), so the Flow page offers only those assets.
    stream_venues = {
        venue for venue, section in symbols_cfg.get("venues", {}).items() if section.get("stream")
    }
    registry = app.extensions["symbols"]
    app.extensions["flow_assets"] = [
        asset for asset in registry.assets() if registry.listings(asset).keys() & stream_venues
    ]
    app.extensions["fills_labels"] = {
        w["address"]: w["label"] for w in app.extensions["tracked_wallets"]
    }
    app.extensions["read_storage"] = storage
    app.extensions["position_reader"] = PositionReader()
    app.extensions["execution_reader"] = ExecutionReader()
    app.extensions["response_cache"] = ResponseCache(ttl_seconds=30)

    auth_module.init_auth(app, storage)
    app.register_blueprint(pages.bp)
    app.register_blueprint(api.bp)
    app.register_blueprint(positions.bp)
    app.register_blueprint(execution.bp)
    app.register_blueprint(margin.bp)

    # Errors keep the site's frame; API routes keep answering in JSON.
    titles = {
        403: ("Not allowed", "Your account can't open this page."),
        404: ("Page not found", "There's nothing at this address."),
        500: ("Something went wrong", "The page failed to load. Try again in a moment."),
    }

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException):
        if request.path.startswith("/api/"):
            return jsonify(error=error.description), error.code
        title, message = titles.get(error.code, (error.name, error.description))
        return render_template("error.html", code=error.code, title=title, message=message), error.code

    @app.errorhandler(500)
    def server_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error="Internal server error"), 500
        title, message = titles[500]
        return render_template("error.html", code=500, title=title, message=message), 500

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(f"{app.static_folder}/img", "favicon-32.png", mimetype="image/png")

    @app.cli.command("create-user")
    @click.argument("email")
    @click.option("--internal", is_flag=True, help="Grant access to /health")
    @click.password_option()
    def create_user_cmd(email: str, internal: bool, password: str) -> None:
        """Create (or update) a web account. The only way accounts are made."""
        auth_module.create_user(storage, email, password, internal)
        click.echo(f"user {email} ready (internal={internal})")

    return app