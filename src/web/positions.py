from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median

from core.positions import asset_id, number

ZERO = Decimal(0)


def _iso(value):
    return value.isoformat() if value is not None else None


def _status(account, kind: str, now: datetime) -> dict:
    checked = account.get(f"{kind}_checked_at")
    error = account.get(f"{kind}_error")
    if error:
        state = "error"
    elif checked is None:
        state = "pending"
    else:
        age = max(0, (now - checked).total_seconds())
        fresh, stale = (120, 300) if kind == "positions" else (600, 900)
        state = "fresh" if age <= fresh else "delayed" if age <= stale else "stale"
    return {
        "state": state,
        "checked_at": _iso(checked),
        "attempted_at": _iso(account.get(f"{kind}_attempted_at")),
        "error": error,
    }


def _amounts(values: dict[str, Decimal]) -> list[dict]:
    return [{"currency": currency, "amount": str(amount)} for currency, amount in sorted(values.items())]


def _sum_amounts(target: dict[str, Decimal], amounts: dict[str, Decimal]) -> None:
    for currency, amount in amounts.items():
        target[currency] += amount


def position_payload(accounts, events, fx, registry, now: datetime, hours: int) -> dict:
    """Value quantities with a common fresh mark; never offset unrelated assets.

    Native ledger denominations survive aggregation. USD funding is an estimate
    at the explicitly timestamped current FX, not historical payment-time USD.
    Incomplete scans or stale/missing inputs cannot produce authoritative totals.
    """
    since = now - timedelta(hours=hours)
    fx = fx or {}
    fx_checked = fx.get("ts")
    fx_fresh = fx_checked is not None and now - fx_checked <= timedelta(minutes=5)
    rates = {"USD": Decimal(1)}
    if fx_fresh:
        for currency, raw in (fx.get("rates") or {}).items():
            rate = number(raw)
            if rate > ZERO:
                rates[currency] = rate

    def usd(amounts):
        if any(currency not in rates for currency in amounts):
            return None
        return sum((amount * rates[currency] for currency, amount in amounts.items()), ZERO)

    public_accounts = []
    by_account = {}
    positions_complete = bool(accounts)
    funding_complete = bool(accounts)
    for account in accounts:
        snapshot = account.get("snapshot") or {}
        position_status = _status(account, "positions", now)
        funding_status = _status(account, "funding", now)
        coverage = account.get("funding_since")
        catching_up = bool(account.get("funding_cursor"))
        position_ok = account.get("snapshot") is not None and position_status["state"] in {"fresh", "delayed"}
        funding_ok = (
            funding_status["state"] in {"fresh", "delayed"}
            and coverage is not None and coverage <= since and not catching_up
        )
        positions_complete = positions_complete and position_ok
        funding_complete = funding_complete and funding_ok
        position_status["complete"] = position_ok
        funding_status["complete"] = funding_ok
        funding_status.update(
            coverage_since=_iso(coverage),
            coverage_note=account.get("funding_coverage_note"),
            catching_up=catching_up,
        )
        public = {
            "id": account["id"], "venue": account["venue"], "address": account["address"],
            "label": account["label"], "created_at": _iso(account.get("created_at")),
            "has_read_only_token": bool(account.get("has_read_only_token")),
            "positions": position_status, "funding": funding_status,
            "balances": snapshot.get("balances", []), "equity": snapshot.get("equity", []),
            "notes": snapshot.get("notes", []),
        }
        public_accounts.append(public)
        by_account[account["id"]] = public

    groups = {}
    legs = {}

    def group_for(symbol):
        asset = asset_id(registry, symbol)
        if asset not in groups:
            groups[asset] = {
                "asset": asset, "label": registry.metadata(asset).label,
                "long": ZERO, "short": ZERO, "pnl": defaultdict(Decimal),
                "funding": defaultdict(Decimal), "prices": {}, "legs": [],
            }
        return groups[asset]

    def leg_for(account, symbol, scope="main"):
        key = (account["id"], symbol, scope)
        if key not in legs:
            group = group_for(symbol)
            venue = registry.venue_identity(
                "rise" if account["venue"] == "risex" else account["venue"], symbol
            )
            leg = {
                "account_id": account["id"], "account_label": account["label"],
                "venue": account["venue"],
                "venue_label": "RISEx" if account["venue"] == "risex" else venue.label,
                "address": account["address"], "scope": scope, "symbol": symbol,
                "quantity": "0" if account["positions"]["checked_at"] else None,
                "entry_price": None, "mark_price": None,
                "quote_currency": None, "settlement_currency": None,
                "collateral_currencies": [], "unrealized_pnl": None,
                "unrealized_pnl_currency": None,
                "funding_amounts": defaultdict(Decimal),
                "state": account["positions"]["state"],
                "checked_at": account["positions"]["checked_at"],
            }
            legs[key] = leg
            group["legs"].append(leg)
        return legs[key]

    for account in accounts:
        public = by_account[account["id"]]
        for position in (account.get("snapshot") or {}).get("positions", []):
            quantity = number(position["quantity"])
            if quantity == ZERO:
                continue
            symbol = position["symbol"]
            scope = position.get("scope", "main")
            leg = leg_for(public, symbol, scope)
            leg.update({
                "quantity": str(quantity), "entry_price": position.get("entry_price"),
                "mark_price": position.get("mark_price"),
                "quote_currency": position["quote_currency"],
                "settlement_currency": position["settlement_currency"],
                "collateral_currencies": position.get("collateral_currencies", []),
                "unrealized_pnl": position.get("unrealized_pnl"),
                "unrealized_pnl_currency": position.get("pnl_currency") or position["settlement_currency"],
            })
            group = group_for(symbol)
            if quantity > ZERO:
                group["long"] += quantity
            else:
                group["short"] -= quantity
            pnl = position.get("unrealized_pnl")
            if pnl is not None:
                group["pnl"][leg["unrealized_pnl_currency"]] += number(pnl)
            quote = position["quote_currency"]
            mark = position.get("mark_price")
            if public["positions"]["state"] in {"fresh", "delayed"} and mark is not None and quote in rates:
                price = number(mark) * rates[quote]
                if price > ZERO:
                    group["prices"][(account["venue"], symbol)] = price

    position_legs = defaultdict(list)
    for leg in legs.values():
        position_legs[(leg["account_id"], leg["symbol"])].append(leg)

    for event in events:
        public = by_account.get(event["account_id"])
        if public is None or not since <= event["ts"] <= now:
            continue
        amount = number(event["amount"])
        currency = event["currency"]
        symbol = event["symbol"]
        group_for(symbol)["funding"][currency] += amount
        scope = event.get("scope", "main")
        candidates = position_legs.get((public["id"], symbol), ())
        leg = candidates[0] if scope == "account-market" and len(candidates) == 1 else leg_for(public, symbol, scope)
        if scope == "account-market":
            leg["funding_scope"] = scope
        leg["funding_amounts"][currency] += amount

    assets = []
    total_funding = defaultdict(Decimal)
    total_pnl = defaultdict(Decimal)
    unmatched_delta = ZERO
    delta_available = positions_complete
    for asset, group in sorted(groups.items()):
        net = group["long"] - group["short"]
        exposure_known = any(leg["quantity"] is not None for leg in group["legs"])
        reference = median(group["prices"].values()) if group["prices"] else None
        delta = (net * reference if reference is not None else ZERO if net == ZERO else None) if exposure_known else None
        if delta is None:
            delta_available = False
        else:
            unmatched_delta += abs(delta)
        asset_legs = []
        for leg in group["legs"]:
            leg["funding"] = _amounts(leg.pop("funding_amounts"))
            asset_legs.append(leg)
        funding_usd = usd(group["funding"]) if funding_complete else None
        assets.append({
            "asset": asset, "label": group["label"],
            "long_quantity": str(group["long"]) if exposure_known else None,
            "short_quantity": str(group["short"]) if exposure_known else None,
            "net_quantity": str(net) if exposure_known else None,
            "reference_price_usd": str(reference) if reference is not None else None,
            "net_delta_usd": str(delta) if delta is not None else None,
            "unrealized_pnl": _amounts(group["pnl"]), "funding": _amounts(group["funding"]),
            "funding_usd": str(funding_usd) if funding_usd is not None else None,
            "partial": not positions_complete or not funding_complete,
            "legs": sorted(asset_legs, key=lambda leg: (leg["account_label"], leg["symbol"], leg["scope"])),
        })
        _sum_amounts(total_funding, group["funding"])
        _sum_amounts(total_pnl, group["pnl"])
    funding_usd = usd(total_funding) if funding_complete else None
    return {
        "as_of": now.isoformat(), "hours": hours,
        "accounts": public_accounts, "assets": assets,
        "totals": {
            "positions_complete": positions_complete, "funding_complete": funding_complete,
            "net_delta_usd": str(unmatched_delta) if delta_available else None,
            "funding": _amounts(total_funding),
            "funding_usd": str(funding_usd) if funding_usd is not None else None,
            "unrealized_pnl": _amounts(total_pnl),
        },
        "fx": {
            "rates": {currency: str(rate) for currency, rate in rates.items()},
            "expired": not fx_fresh,
            "checked_at": _iso(fx_checked), "source": fx.get("source", "Coinbase USD exchange rates"),
        },
    }
