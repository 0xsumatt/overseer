from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from core.positions import CurrencyAmount, FundingEvent, FundingPage, Position, PositionReadError, PositionSnapshot, number
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter

_BASE = "https://api.rise.trade"
_NS = 1_000_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _address(value: str) -> str:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise PositionReadError("RISEx requires a 0x-prefixed account address.")
    return value.lower()


def _ns(value: datetime) -> int:
    if value.tzinfo is None:
        raise PositionReadError("Funding requires timezone-aware dates.")
    delta = value.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * _NS + delta.microseconds * 1000


def _data(payload: Any) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise PositionReadError("RISEx returned a missing account response envelope.")
    return payload["data"]


class RisexPositions:
    """Portfolio/details and funding-payments report HUMAN decimal units.

    Do not substitute /v1/positions: that endpoint exposes raw fixed-point
    integers, unlike the populated production portfolio/details response.
    """

    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient(limiter=RateLimiter.per_minute(60, burst=2))
        self._markets: dict[str, tuple[str, str]] | None = None
        self._markets_at = 0.0

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _market_map(self) -> dict[str, tuple[str, str]]:
        if self._markets is None or time.monotonic() - self._markets_at >= 1800:
            system = _data(await self.http.get_json(f"{_BASE}/v1/system/config"))
            payload = _data(await self.http.get_json(f"{_BASE}/v1/markets"))
            try:
                usdc = system["addresses"]["usdc"].lower()
                rows = payload["markets"]
                if not isinstance(rows, list) or not rows or not re.fullmatch(r"0x[0-9a-f]{40}", usdc):
                    raise ValueError
                mapping = {}
                for row in rows:
                    config = row["config"]
                    # Resolve the token address, not the market name's suffix.
                    if config["quote"].lower() != usdc:
                        raise PositionReadError("RISEx returned an unsupported settlement token.")
                    if not isinstance(config["name"], str) or not config["name"]:
                        raise ValueError
                    mapping[str(row["market_id"])] = (config["name"], "USDC")
                self._markets = mapping
                self._markets_at = time.monotonic()
            except (KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, PositionReadError):
                    raise
                raise PositionReadError("RISEx returned malformed market configuration.") from exc
        return self._markets

    async def resolve_accounts(self, wallet: str, *, cursor: str | None = None) -> dict:
        wallet = _address(wallet)
        if cursor is not None:
            raise PositionReadError("RISEx account discovery does not use a cursor.")
        # The public API exposes account-address reads, not an owner registry.
        # Session signers are authorization keys, not independent accounts.
        return {
            "wallet": wallet, "accounts": [{"address": wallet, "name": "Account"}], "next_cursor": None,
            "note": "RISEx exposes no public wallet-to-subaccount discovery. Only this account address is included; other addresses are not automatically discovered.",
        }

    async def fetch_positions(self, address: str) -> PositionSnapshot:
        address = _address(address)
        markets = await self._market_map()
        data = _data(await self.http.get_json(f"{_BASE}/v1/portfolio/details", params={"account": address}))
        try:
            if _address(data["account"]) != address or not isinstance(data["positions"], list):
                raise ValueError
            summary = data["summary"]
            snapshot = PositionSnapshot(
                balances=[CurrencyAmount("USDC", number(summary["usdc_balance"]))],
                equity=[CurrencyAmount("USDC", number(summary["total_account_value"]))],
            )
            seen: set[str] = set()
            for row in data["positions"]:
                quantity = number(row["size"])
                if not quantity:
                    continue
                market_id = str(row["market_id"])
                if market_id in seen or row["margin_mode"] not in (0, 1):
                    raise ValueError
                seen.add(market_id)
                symbol, currency = markets[market_id]
                snapshot.positions.append(Position(
                    symbol=symbol, quantity=quantity, quote_currency=currency,
                    settlement_currency=currency, collateral_currencies=("USDC",),
                    entry_price=number(row["avg_entry_price"]) if row.get("avg_entry_price") not in (None, "") else None,
                    mark_price=number(row["mark_price"]) if row.get("mark_price") not in (None, "") else None,
                    unrealized_pnl=number(row["unrealized_pnl"]) if row.get("unrealized_pnl") not in (None, "") else None,
                    scope=f"isolated:{market_id}" if row["margin_mode"] == 1 else "main",
                    pnl_currency=currency,
                ))
            snapshot.notes.append("RISEx portfolio equity is the venue's total account value; isolated margin is already included and is not added again. Funding history excludes unsettled accruals and dividend adjustments and reports account/market totals without historical margin mode.")
            return snapshot
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            if isinstance(exc, PositionReadError):
                raise
            raise PositionReadError("RISEx returned incomplete or malformed portfolio state.") from exc

    async def fetch_funding(self, address: str, since: datetime, until: datetime, *, cursor: dict[str, Any] | None = None, token: str | None = None) -> FundingPage:
        address = _address(address)
        start, end = _ns(since), _ns(until)
        if start >= end:
            raise PositionReadError("Funding requires an ordered time window.")
        page_number = 1
        if cursor is not None:
            if cursor.get("address") != address or cursor.get("start") != str(start) or cursor.get("end") != str(end) or cursor.get("order") != "-time" or type(cursor.get("page")) is not int or cursor["page"] < 2:
                raise PositionReadError("RISEx funding cursor does not match this account and window.")
            page_number = cursor["page"]
        markets = await self._market_map()
        data = _data(await self.http.get_json(f"{_BASE}/v1/account/funding-payments", params={
            "account": address, "start_time": str(start), "end_time": str(end),
            "page": str(page_number), "limit": "500", "sorted_by": "-time",
        }))
        try:
            rows = data["records"]
            if _address(data["account"]) != address or int(data["page"]) != page_number or not isinstance(rows, list) or not isinstance(data["has_next_page"], bool):
                raise ValueError
            # Detect servers ignoring the requested page; hash excludes request_id.
            fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if cursor is not None and fingerprint == cursor.get("fingerprint"):
                raise PositionReadError("RISEx repeated a funding page; retained history is incomplete until the venue returns progressing pages.")
            if data["has_next_page"] and not rows:
                raise ValueError
            events: dict[str, FundingEvent] = {}
            for row in rows:
                if not isinstance(row["is_settled"], bool) or row["entry_type"] not in ("funding", "dividend"):
                    raise ValueError
                if not row["is_settled"] or row["entry_type"] != "funding":
                    continue
                if not row["tx_hash"] or int(row["block_number"]) <= 0:
                    raise ValueError
                timestamp = int(row["block_time"])
                if not start <= timestamp < end:
                    continue
                symbol, currency = markets[str(row["market_id"])]
                # No ledger id/log index is exposed. Use immutable settlement
                # identity, never page offset (which changes on subsequent scans).
                identity = [row["tx_hash"].lower(), str(row["market_id"]), str(timestamp), row["side"], row["trade_side"], row["position_size"], row["funding_payment"]]
                event_id = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
                ts = _EPOCH + timedelta(seconds=timestamp // _NS, microseconds=(timestamp % _NS) // 1000)
                event = FundingEvent(event_id, ts, symbol, -number(row["funding_payment"]), currency, scope="account-market")
                if event_id in events and events[event_id] != event:
                    raise ValueError
                events[event_id] = event
            continuation = {"address": address, "start": str(start), "end": str(end), "order": "-time", "page": page_number + 1, "fingerprint": fingerprint} if data["has_next_page"] else None
            return FundingPage(list(events.values()), continuation, not data["has_next_page"], coverage_since=since, coverage_note="RISEx settled regular funding only; positive-paid venue amounts are negated. The API supplies no retention floor or settlement log index; coverage is limited to records returned for this fixed time window.")
        except (KeyError, TypeError, ValueError, ArithmeticError, OverflowError) as exc:
            if isinstance(exc, PositionReadError):
                raise
            raise PositionReadError("RISEx returned malformed funding history or a non-progressing page.") from exc
