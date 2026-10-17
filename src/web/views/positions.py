from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from uuid import UUID

from flask import Blueprint, current_app, jsonify, request

from core.positions import PositionReadError
from core.position_tokens import validate_token
from web.position_reader import ReaderBusy
from web.positions import position_payload

bp = Blueprint("position_api", __name__, url_prefix="/api/positions")
_VENUES = {"hyperliquid", "lighter", "bulk", "risex"}
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _invalid():
    raise PositionReadError("Invalid position request. Check the supplied fields.")


def _object(value, allowed, required=()):
    if not isinstance(value, dict) or value.keys() - set(allowed) or not set(required) <= value.keys():
        _invalid()
    return dict(value)

def _body(allowed, required=()):
    limit = 8 * 1024 * 1024 if request.endpoint == "position_api.aggregate" else 64 * 1024
    request.max_content_length = limit
    if request.content_length is not None and request.content_length > limit:
        _invalid()
    if request.args:
        _invalid()
    try:
        body = request.get_json(silent=True)
    except RecursionError:
        _invalid()
    return _object(body, allowed, required)


def _text(value, limit=128):
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(char) < 32 for char in value):
        _invalid()
    return value


def _list(value, limit):
    if not isinstance(value, list) or len(value) > limit:
        _invalid()
    return value


def _date(value, now, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > 40:
        _invalid()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            _invalid()
        parsed = parsed.astimezone(timezone.utc)
        if not datetime(2000, 1, 1, tzinfo=timezone.utc) <= parsed <= now + timedelta(minutes=5):
            _invalid()
        return parsed
    except (ValueError, OverflowError):
        _invalid()


def _number(value, positive=False):
    if not isinstance(value, str) or len(value) > 96 or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?", value):
        _invalid()
    try:
        number = Decimal(value)
        if not number.is_finite() or abs(number.as_tuple().exponent) > 60 or abs(number.adjusted()) > 60 or len(number.as_tuple().digits) > 80 or (positive and number <= 0):
            _invalid()
    except InvalidOperation:
        _invalid()
    return value


def _venue(value):
    if not isinstance(value, str) or value not in _VENUES:
        _invalid()
    return value


def _address(venue, raw):
    if not isinstance(raw, str) or len(raw) > 64:
        _invalid()
    address = raw.strip()
    if venue in {"hyperliquid", "risex"}:
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
            _invalid()
        return address.lower()
    if venue == "lighter":
        if not re.fullmatch(r"[0-9]{1,15}", address) or int(address) > 2**48 - 1:
            _invalid()
        return str(int(address))
    if not 32 <= len(address) <= 44 or any(char not in _BASE58 for char in address):
        _invalid()
    value = 0
    for char in address:
        value = value * 58 + _BASE58.index(char)
    if len(address) - len(address.lstrip("1")) + (value.bit_length() + 7) // 8 != 32:
        _invalid()
    return address


def _hours(value):
    if type(value) is not int or value not in {24, 168, 720}:
        _invalid()
    return value


def _cursor(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        _invalid()
    def visit(item, depth=0):
        if depth > 6:
            _invalid()
        if isinstance(item, dict):
            if len(item) > 32:
                _invalid()
            for key, child in item.items():
                _text(key, 80)
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in _list(item, 1000):
                visit(child, depth + 1)
        elif isinstance(item, str):
            _text(item, 512)
        elif item is not None and type(item) not in {bool, int}:
            _invalid()
        elif type(item) is int and abs(item) > 10**22:
            _invalid()
    visit(value)
    if len(json.dumps(value)) > 65536:
        _invalid()
    return value


def _scan(value, now, hours):
    scan = _object(value, {"since", "until", "cursor", "coverage_since", "coverage_note"}, {"since", "until", "coverage_since"})
    for key in ("since", "until", "coverage_since"):
        scan[key] = _date(scan[key], now)
    if scan["until"] > now or scan["until"] - scan["since"] != timedelta(hours=hours) or not scan["since"] <= scan["coverage_since"] <= scan["until"]:
        _invalid()
    scan["cursor"] = _cursor(scan.get("cursor"))
    if scan.get("coverage_note") is not None:
        _text(scan["coverage_note"], 1000)
    return scan


def _snapshot(value):
    if value is None:
        return None
    result = _object(value, {"positions", "balances", "equity", "notes"}, {"positions", "balances", "equity", "notes"})
    rows = []
    identities = set()
    for raw in _list(result["positions"], 1000):
        row = _object(raw, {"symbol", "quantity", "quote_currency", "settlement_currency", "collateral_currencies", "mark_price", "entry_price", "unrealized_pnl", "scope", "pnl_currency"}, {"symbol", "quantity", "quote_currency", "settlement_currency", "collateral_currencies"})
        for key in ("symbol", "quote_currency", "settlement_currency"):
            _text(row[key])
        for key in ("scope", "pnl_currency"):
            if row.get(key) is not None:
                _text(row[key])
        _text(row.get("scope", "main"))
        identity = (row["symbol"], row.get("scope", "main"))
        if identity in identities:
            _invalid()
        identities.add(identity)
        _number(row["quantity"])
        for key in ("mark_price", "entry_price", "unrealized_pnl"):
            if row.get(key) is not None:
                _number(row[key])
        for currency in _list(row["collateral_currencies"], 32):
            _text(currency)
        rows.append(row)
    result["positions"] = rows
    for key in ("balances", "equity"):
        for amount in _list(result[key], 128):
            _object(amount, {"currency", "amount"}, {"currency", "amount"})
            _text(amount["currency"])
            _number(amount["amount"])
    for note in _list(result["notes"], 32):
        _text(note, 1000)
    return result


@bp.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.errorhandler(PositionReadError)
def invalid_request(error):
    return jsonify(error=str(error)), 503 if isinstance(error, ReaderBusy) else 400


@bp.errorhandler(413)
def oversized_request(_error):
    return jsonify(error="Position request is too large."), 413


def _reader_call(method, *args):
    try:
        return getattr(current_app.extensions["position_reader"], method)(*args)
    except ReaderBusy:
        return jsonify(error="Position reader is busy. Try again shortly."), 503
    except PositionReadError as exc:
        return jsonify(error=str(exc)), 502
    except Exception:
        return jsonify(error="Position data is unavailable. Try again shortly."), 502


@bp.post("/read")
def read():
    body = _body({"venue", "address", "kind", "hours", "scan", "read_only_token"}, {"venue", "address", "kind"})
    venue = _venue(body["venue"])
    address = _address(venue, body["address"])
    kind = body["kind"]
    if kind not in ("positions", "funding"):
        _invalid()
    token = None
    if "read_only_token" in body:
        if venue != "lighter" or kind != "funding":
            _invalid()
        token = validate_token(body["read_only_token"], address)
    now = datetime.now(timezone.utc)
    since = until = cursor = None
    if kind == "funding":
        hours = _hours(body.get("hours", 24))
        scan = _scan(body["scan"], now, hours) if body.get("scan") is not None else {
            "since": now.replace(microsecond=0) - timedelta(hours=hours), "until": now.replace(microsecond=0), "cursor": None,
            "coverage_since": now.replace(microsecond=0) - timedelta(hours=hours), "coverage_note": None,
        }
        since, until, cursor = scan["since"], scan["until"], scan["cursor"]
    elif "scan" in body or "hours" in body:
        _invalid()
    result = _reader_call("read", venue, address, kind, since, until, cursor, token)
    if isinstance(result, tuple):
        return result
    checked = datetime.now(timezone.utc).isoformat()
    if kind == "positions":
        return jsonify(attempted_at=now.isoformat(), checked_at=checked, snapshot=result)
    coverage = _date(result["coverage_since"], datetime.now(timezone.utc), nullable=True)
    if coverage is not None:
        scan["coverage_since"] = max(scan["coverage_since"], coverage)
    scan.update(cursor=result["cursor"], coverage_note=result["coverage_note"])
    return jsonify(attempted_at=now.isoformat(), checked_at=scan["until"].isoformat() if result["complete"] else None,
                   events=result["events"], complete=result["complete"], scan={
                       key: value.isoformat() if isinstance(value, datetime) else value for key, value in scan.items()})


@bp.get("/fx")
def fx():
    if request.args:
        _invalid()
    result = _reader_call("fx")
    return result if isinstance(result, tuple) else jsonify(result)


@bp.post("/accounts")
def discover_accounts():
    body = _body({"venue", "wallet", "cursor"}, {"venue", "wallet"})
    venue = _venue(body["venue"])
    wallet_venue = "hyperliquid" if venue == "lighter" and isinstance(body["wallet"], str) and body["wallet"].strip().startswith("0x") else venue
    wallet = _address(wallet_venue, body["wallet"])
    cursor = body.get("cursor")
    if cursor is not None:
        _text(cursor, 2048)
    result = _reader_call("discover_accounts", venue, wallet, cursor)
    return result if isinstance(result, tuple) else jsonify(result)


@bp.post("/aggregate")
def aggregate():
    body = _body({"hours", "accounts", "events", "fx"}, {"hours", "accounts", "events", "fx"})
    hours = _hours(body["hours"])
    now = datetime.now(timezone.utc)
    accounts = []
    ids = set()
    addresses = set()
    fields = {"id", "venue", "address", "label", "created_at", "snapshot", "has_read_only_token", "positions_checked_at", "positions_attempted_at", "positions_error", "funding_checked_at", "funding_attempted_at", "funding_error", "funding_since", "funding_cursor", "funding_coverage_note"}
    for raw in _list(body["accounts"], 32):
        account = _object(raw, fields, {"id", "venue", "address", "label", "created_at", "snapshot", "has_read_only_token"})
        try:
            account["id"] = str(UUID(_text(account["id"], 36)))
        except ValueError:
            _invalid()
        account["venue"] = _venue(account["venue"])
        account["address"] = _address(account["venue"], account["address"])
        identity = (account["venue"], account["address"])
        if account["id"] in ids or identity in addresses or type(account["has_read_only_token"]) is not bool:
            _invalid()
        if account["has_read_only_token"] and account["venue"] != "lighter":
            _invalid()
        ids.add(account["id"])
        addresses.add(identity)
        _text(account["label"], 80)
        account["created_at"] = _date(account["created_at"], now)
        account["snapshot"] = _snapshot(account["snapshot"])
        for key in ("positions_checked_at", "positions_attempted_at", "funding_checked_at", "funding_attempted_at", "funding_since"):
            account[key] = _date(account.get(key), now, nullable=True)
        for key in ("positions_error", "funding_error"):
            if account.get(key) is not None:
                _text(account[key], 1000)
                account[key] = "Position read failed. Try again shortly."
        if account.get("funding_coverage_note") is not None:
            _text(account["funding_coverage_note"], 1000)
        if account.get("funding_cursor") is not None:
            account["funding_cursor"] = _scan(account["funding_cursor"], now, hours)
        accounts.append(account)
    events = []
    seen = {}
    for raw in _list(body["events"], 50000):
        event = _object(raw, {"account_id", "event_id", "ts", "symbol", "amount", "currency", "scope"}, {"account_id", "event_id", "ts", "symbol", "amount", "currency"})
        _text(event["account_id"], 36)
        if event["account_id"] not in ids:
            _invalid()
        for key in ("event_id", "symbol", "currency"):
            _text(event[key], 512 if key == "event_id" else 128)
        if "scope" in event:
            _text(event["scope"])
        event["ts"] = _date(event["ts"], now)
        if event["ts"] > now:
            _invalid()
        _number(event["amount"])
        key = (event["account_id"], event["event_id"])
        if key in seen:
            if event != seen[key]:
                _invalid()
            continue
        seen[key] = event
        events.append(event)
    fx_data = body["fx"]
    if fx_data is not None:
        fx_data = _object(fx_data, {"ts", "rates", "source"}, {"ts", "rates", "source"})
        fx_data["ts"] = _date(fx_data["ts"], now)
        _text(fx_data["source"], 128)
        rates = fx_data["rates"]
        if not isinstance(rates, dict) or len(rates) > 1000:
            _invalid()
        for currency, rate in rates.items():
            _text(currency)
            _number(rate, positive=True)
    return jsonify(position_payload(accounts, events, fx_data, current_app.extensions["symbols"], now, hours))
