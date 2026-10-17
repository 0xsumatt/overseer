from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

from core.positions import CurrencyAmount, FundingEvent, FundingPage, Position, PositionReadError, PositionSnapshot, number
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter

_BASE = "https://mainnet-api1.bulk.trade/api/v1"
_NS = 1_000_000_000


def _address(value: str) -> str:
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", value):
        raise PositionReadError("Bulk requires a base58 account public key.")
    decoded = 0
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    for char in value:
        decoded = decoded * 58 + alphabet.index(char)
    leading = len(value) - len(value.lstrip("1"))
    if leading + (decoded.bit_length() + 7) // 8 != 32:
        raise PositionReadError("Bulk requires a 32-byte account public key.")
    return value


def _window(since: datetime, until: datetime) -> tuple[str, str]:
    if since.tzinfo is None or until.tzinfo is None or since >= until:
        raise PositionReadError("Funding requires an ordered timezone-aware window.")
    return since.astimezone(timezone.utc).isoformat(), until.astimezone(timezone.utc).isoformat()


class BulkPositions:
    """Unsigned mainnet reads; fullAccount includes attached isolated lanes once."""

    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient(limiter=RateLimiter.per_minute(60, burst=2))
        self._markets: dict[str, str] | None = None
        self._markets_at = 0.0

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _market_map(self) -> dict[str, str]:
        if self._markets is None or time.monotonic() - self._markets_at >= 1800:
            rows = await self.http.get_json(f"{_BASE}/exchangeInfo")
            if not isinstance(rows, list) or not rows:
                raise PositionReadError("Bulk market configuration is unavailable.")
            try:
                mapping = {row["symbol"]: row["quoteAsset"] for row in rows}
                if any(not isinstance(k, str) or not isinstance(v, str) or not k or not v for k, v in mapping.items()):
                    raise ValueError
            except (KeyError, TypeError, ValueError) as exc:
                raise PositionReadError("Bulk returned malformed market configuration.") from exc
            self._markets = mapping
            self._markets_at = time.monotonic()
        return self._markets

    async def resolve_accounts(self, wallet: str, *, cursor: str | None = None) -> dict:
        wallet = _address(wallet)
        if cursor is not None:
            raise PositionReadError("Bulk account discovery does not use a cursor.")
        response = await self.http.post_json(f"{_BASE}/account", json={"type": "fullAccount", "user": wallet})
        try:
            if not isinstance(response, list) or len(response) != 1:
                raise ValueError
            account = response[0]["fullAccount"]
            children = account["subAccounts"]
            if not isinstance(children, list):
                raise ValueError
            accounts = [{"address": wallet, "name": "Main account"}]
            seen = {wallet}
            # Separate subaccounts are owned children. Attached isolated lanes
            # already belong to this account's positions and margin snapshot.
            for child in children:
                address = _address(child["pubkey"])
                if address in seen:
                    raise ValueError
                name = child.get("name")
                if name is not None and not isinstance(name, str):
                    raise ValueError
                name = "".join(char for char in (name or "") if ord(char) >= 32 and ord(char) != 127).strip()[:80]
                accounts.append({"address": address, "name": name or f"Subaccount {len(accounts)}"})
                seen.add(address)
        except (KeyError, TypeError, ValueError):
            raise PositionReadError("Bulk returned an inconsistent owned subaccount list.") from None
        return {"wallet": wallet, "accounts": accounts, "next_cursor": None}

    async def fetch_positions(self, address: str) -> PositionSnapshot:
        address = _address(address)
        markets = await self._market_map()
        response = await self.http.post_json(f"{_BASE}/account", json={"type": "fullAccount", "user": address})
        try:
            if not isinstance(response, list) or len(response) != 1:
                raise ValueError
            account = response[0]["fullAccount"]
            rows, margin = account["positions"], account["margin"]
            if not isinstance(rows, list) or not isinstance(margin, dict):
                raise ValueError
            snapshot = PositionSnapshot()
            seen: set[tuple[str, str]] = set()
            isolated = False
            for row in rows:
                scope = _address(row["isoPubkey"]) if row["iso"] is True else "main"
                if not isinstance(row["iso"], bool):
                    raise ValueError
                isolated |= scope != "main"
                symbol = row["symbol"]
                key = (scope, symbol)
                if key in seen:
                    raise ValueError
                seen.add(key)
                quantity = number(row["size"])
                if not quantity:
                    continue
                snapshot.positions.append(Position(
                    symbol=symbol, quantity=quantity, quote_currency=markets[symbol],
                    settlement_currency="USDC", collateral_currencies=("USDC",),
                    entry_price=number(row["price"]) if row.get("price") is not None else None,
                    mark_price=number(row["fairPrice"]) if row.get("fairPrice") is not None else None,
                    unrealized_pnl=number(row["unrealizedPnl"]) if row.get("unrealizedPnl") is not None else None,
                    scope=scope,
                    pnl_currency="USD",
                ))
            # agg* already includes the parent: never add local margin to it.
            if "aggTotalMargin" in margin:
                snapshot.equity.append(CurrencyAmount("UNSPECIFIED", number(margin["aggTotalMargin"])))
            elif isolated:
                snapshot.notes.append("Bulk aggregate margin is unavailable; attached isolated exposures are included, but combined equity is unknown.")
            else:
                snapshot.equity.append(CurrencyAmount("UNSPECIFIED", number(margin["totalMargin"])))
            children = account.get("subAccounts", [])
            if not isinstance(children, list):
                raise ValueError
            for child in children:
                snapshot.notes.append(f"Separate Bulk subaccount {_address(child['pubkey'])} is not included in this snapshot; add the master wallet to discover its owned accounts.")
            snapshot.notes.append("Bulk price quotes and position PnL are USD; collateral and settlement are USDC. The margin fields do not tag their denomination: equity is retained as UNSPECIFIED and is not converted or assumed to equal USD/USDC.")
            return snapshot
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            if isinstance(exc, PositionReadError):
                raise
            raise PositionReadError("Bulk returned incomplete or malformed account state.") from exc

    async def fetch_funding(self, address: str, since: datetime, until: datetime, *, cursor: dict[str, Any] | None = None, token: str | None = None) -> FundingPage:
        address = _address(address)
        start, end = _window(since, until)
        request: dict[str, Any] = {"type": "fundingHistory", "user": address, "limit": 500}
        if cursor is not None:
            if cursor.get("address") != address or cursor.get("since") != start or cursor.get("until") != end or not isinstance(cursor.get("next"), str) or not cursor["next"]:
                raise PositionReadError("Bulk funding cursor does not match this account and window.")
            request["cursor"] = cursor["next"]
        payload = await self.http.post_json(f"{_BASE}/account", json=request)
        try:
            rows, page = payload["data"], payload["page"]
            if not isinstance(rows, list) or not isinstance(page["hasMore"], bool) or page["coverage"] not in {"complete", "partial", "unknown"}:
                raise ValueError
            fixed_slots = {key: int(page[key]) for key in ("asOfSlot", "startSlot", "endSlot")}
            if cursor is not None and cursor.get("slots") != fixed_slots:
                raise PositionReadError("Bulk funding history changed its fixed slot window; restart the scan.")
            coverage = page["coverage"]
            if cursor is not None and cursor.get("coverage") in ("partial", "unknown"):
                coverage = cursor["coverage"]
            next_cursor = page["nextCursor"]
            if page["hasMore"]:
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor == request.get("cursor") or not rows:
                    raise ValueError
            elif next_cursor is not None:
                raise ValueError
            events: dict[str, FundingEvent] = {}
            oldest = cursor.get("oldest") if cursor is not None else None
            if oldest is not None:
                oldest = datetime.fromisoformat(oldest)
                if oldest.tzinfo is None:
                    raise ValueError
            for row in rows:
                timestamp = int(row["timestamp"])
                ts = datetime.fromtimestamp(timestamp // _NS, timezone.utc).replace(microsecond=(timestamp % _NS) // 1000)
                oldest = ts if oldest is None else min(oldest, ts)
                scope = _address(row["isoPubkey"]) if row["iso"] is True else "main"
                if not isinstance(row["iso"], bool):
                    raise ValueError
                owner = _address(row["owner"])
                event_id = f"{owner}:{int(row['slot'])}:{int(row['sequence'])}"
                event = FundingEvent(event_id, ts, row["symbol"], number(row["payment"]), "USD", scope)
                if since <= ts < until:
                    if event_id in events and events[event_id] != event:
                        raise ValueError
                    events[event_id] = event
            note = f"Bulk retained history coverage: {coverage}; slots {page['startSlot']}–{page['endSlot']} (snapshot {page['asOfSlot']})."
            if page.get("minAvailableSlot") is not None:
                note += f" Earliest authoritative retained slot: {page['minAvailableSlot']}; no timestamp mapping is supplied, so full requested time-window coverage is unproven."
            else:
                note += " Retention floor is unknown; empty history does not prove zero funding before retained history."
            continuation = {"address": address, "since": start, "until": end, "next": next_cursor, "oldest": oldest.isoformat() if oldest is not None else None, "slots": fixed_slots, "coverage": coverage} if page["hasMore"] else None
            # Slot retention has no authoritative timestamp conversion. When
            # coverage is complete, the oldest returned event provides a safe
            # (possibly overly conservative) timestamp bound; partial/unknown
            # coverage proves no continuous requested interval.
            coverage_since = min(until, max(since, oldest)) if coverage == "complete" and oldest is not None else until
            return FundingPage(list(events.values()), continuation, not page["hasMore"], coverage_since=coverage_since, coverage_note=note)
        except (KeyError, TypeError, ValueError, ArithmeticError, OverflowError, OSError) as exc:
            if isinstance(exc, PositionReadError):
                raise
            raise PositionReadError("Bulk returned malformed funding history or a non-progressing cursor.") from exc
