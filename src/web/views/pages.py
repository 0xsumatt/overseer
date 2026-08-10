"""HTML pages: login/logout, candle dashboard, basis view, internal health."""

from __future__ import annotations

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, url_for)
from flask_login import login_required, login_user, logout_user

from web.auth import role_required, verify_credentials

bp = Blueprint("pages", __name__)


def _store():
    return current_app.extensions["read_storage"]


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
    # data comes client-side from /api/trade-flow; the page is just the shell
    registry = current_app.extensions["symbols"]
    return render_template("flow.html", assets=registry.assets())


@bp.get("/wallets")
def wallets():
    tracked = current_app.extensions.get("tracked_wallets", [])
    store = _store()
    symbols = store.wallet_symbols([w["address"] for w in tracked]) if tracked else []
    summaries = [(w["label"], w["venue"], store.fills_summary(w["address"]))
                 for w in tracked]
    return render_template("wallets.html", wallets=tracked,
                           symbols=symbols, summaries=summaries)


@bp.get("/health")
@role_required("can_view_internal")
def health():
    return render_template(
        "health.html",
        freshness=_store().freshness(),
        jobs=_store().job_health(),
    )