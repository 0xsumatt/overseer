"""HTML pages: login/logout, candle dashboard, basis view, internal health."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, url_for)
from flask_login import login_required, login_user, logout_user

from web.auth import role_required, verify_credentials
from core.depth import HEATMAP_WINDOWS, WINDOW_BPS
from core.execution import MAX_BOOK_AGE_SECONDS, NOTIONALS
from core.execution_fees import FEE_SCHEDULES, execution_listings
from core.execution_funding import HOLDS
from core.execution_history import HISTORY_ASSETS, HISTORY_NOTIONALS, HISTORY_WINDOWS_DAYS
from data_collection.exchanges.registry import REGISTRY
from web.views.execution import execution_assets

bp = Blueprint("pages", __name__)


def _store():
    return current_app.extensions["read_storage"]


@bp.app_context_processor
def _venue_labels():
    return {
        "venue_labels": current_app.extensions["symbols"].venue_labels(),
    }


@bp.app_template_filter("utc_time")
def _utc_time(value: datetime | None) -> str:
    """Same format as Fmt.dateTime in format.js: `17 Oct 15:51:43 UTC`."""
    if value is None:
        return "—"
    value = value.astimezone(UTC)
    return f"{value.day} {value:%b %H:%M:%S} UTC"


@bp.app_template_filter("age")
def _age(value: timedelta | None) -> str:
    """Same buckets as Fmt.age in format.js: 12s, 3m 05s, 14m, 2h, 3d."""
    if value is None:
        return "—"
    seconds = max(0, int(value.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 300:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = verify_credentials(
            _store(), request.form.get("email", ""), request.form.get("password", "")
        )
        if user is not None:
            login_user(user)
            return redirect(request.args.get("next") or url_for("pages.dashboard"))
        flash("Invalid email or password.")
    return render_template("login.html")


@bp.get("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("pages.login"))


@bp.get("/")
def dashboard():
    # annotate each series with its canonical asset so the chart's token
    # dropdown groups venue-native symbols ("BTC/USDT", "BTC", "BTC-USD")
    # under one entry; unmapped symbols fall back to their raw name.
    registry = current_app.extensions["symbols"]
    series = [dict(s, asset=registry.asset_for(s["symbol"]) or s["symbol"])
              for s in _store().series_list()]
    return render_template("dashboard.html", series=series)


@bp.get("/basis")
def basis():
    return render_template("basis.html", series=_store().series_list())


@bp.get("/funding")
def funding():
    # data comes client-side from /api/funding; the page is just the shell
    return render_template("funding.html")


@bp.get("/flow")
def flow():
    # data comes client-side from /api/trade-flow; only assets a streaming
    # venue lists can have any
    return render_template("flow.html", assets=current_app.extensions["flow_assets"])


@bp.get("/wallets")
def wallets():
    tracked = current_app.extensions.get("tracked_wallets", [])
    store = _store()
    symbols = store.wallet_symbols([w["address"] for w in tracked]) if tracked else []
    summaries = [(w["label"], w["venue"], store.fills_summary(w["address"]))
                 for w in tracked]
    return render_template("wallets.html", wallets=tracked,
                           symbols=symbols, summaries=summaries)


@bp.get("/positions")
def positions():
    response = current_app.make_response(render_template("positions.html"))
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.get("/execution")
def execution():
    assets = execution_assets(current_app.extensions["symbols"])
    offered = {asset["id"] for asset in assets}
    config = {
        "assets": assets,
        "fee_schedules": FEE_SCHEDULES,
        "notionals": [str(value) for value in NOTIONALS],
        "max_book_age_seconds": MAX_BOOK_AGE_SECONDS,
        "holds": [list(hold) for hold in HOLDS],
        "endpoint": url_for("execution_api.quote"),
        "history": {
            "assets": [asset for asset in HISTORY_ASSETS if asset in offered],
            "windows_days": list(HISTORY_WINDOWS_DAYS),
            "notionals": [str(value) for value in HISTORY_NOTIONALS],
            "endpoint": url_for("api.execution_history"),
        },
    }
    response = current_app.make_response(render_template("execution.html", execution_config=config))
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.get("/margin")
def margin():
    config = {
        "assets": execution_assets(current_app.extensions["symbols"]),
        "holds": [list(hold) for hold in HOLDS],
        "fee_schedules": FEE_SCHEDULES,
        "endpoint": url_for("margin_api.calculate"),
    }
    response = current_app.make_response(render_template("margin.html", margin_config=config))
    response.headers["Cache-Control"] = "private, no-store"
    return response



@bp.get("/depth")
def depth():
    registry = current_app.extensions["symbols"]
    offered = {asset["id"] for asset in execution_assets(registry)}
    config = {
        "assets": [
            {"id": asset, "venues": [
                {"id": venue, "label": FEE_SCHEDULES[venue]["label"],
                 "identity": registry.venue_identity(REGISTRY[venue].exchange, symbol).key}
                for venue, symbol in execution_listings(registry, asset).items()
            ]}
            for asset in HISTORY_ASSETS if asset in offered
        ],
        "windows": [[hours, label] for hours, _step, label in HEATMAP_WINDOWS],
        "window_bps": WINDOW_BPS,
        "endpoint": url_for("api.depth_heatmap"),
    }
    return render_template("depth.html", depth_config=config)


@bp.get("/health")
@role_required("can_view_internal")
def health():
    return render_template(
        "health.html",
        freshness=_store().freshness(),
        jobs=_store().job_health(),
    )