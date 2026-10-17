from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from core.positions import (
    CurrencyAmount, FundingEvent, FundingPage, Position, PositionReadError,
    PositionSnapshot, number,
)
from data_collection.http import HttpClient, HttpError
from data_collection.ratelimit import RateLimiter

_BASE = "https://mainnet.zklighter.elliot.ai/api/v1/"
_METADATA_TTL = 1800


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise PositionReadError("Lighter returned malformed account data.")
    return value


def _array(value: Any) -> list:
    if not isinstance(value, list):
        raise PositionReadError("Lighter returned malformed account data.")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PositionReadError("Lighter returned malformed market metadata.")
    return value


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PositionReadError("Lighter returned malformed account data.")
    return value


def _index(value: str) -> str:
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r"[0-9]+", value) or int(value) > 2**48 - 1:
        raise PositionReadError("Lighter requires a numeric account index.")
    return str(int(value))


def _seconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PositionReadError("Funding dates must include a timezone.")
    return int(value.timestamp())


class LighterPositions:
    """Public account reads and settled positionFunding with optional ro tokens.

    Official schemas: apidocs.lighter.xyz/reference/{account-1,positionfunding,
    orderbookdetails,assetdetails}. REST account sizes and money are human units,
    unlike integer transaction inputs. sign is separate from positive size.
    Mainnet perps use USDC accounting; quote_asset_id=0 is a perp sentinel,
    not spot asset ID 0. Spot balances never become perpetual positions.
    """

    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http if http is not None else HttpClient(
            limiter=RateLimiter.per_minute(60, burst=2)
        )
        self._metadata_at = 0.0
        self._markets: dict[int, dict] = {}
        self._assets: dict[int, dict] = {}
        self._metadata_lock = asyncio.Lock()

    async def _get(self, endpoint: str, *, params: dict[str, str] | None = None, token: str | None = None) -> dict:
        try:
            # Token is never a URL parameter, cached metadata, or error text.
            response = await self.http.get_json(
                _BASE + endpoint, params=params,
                headers={"Authorization": token} if token is not None else None,
            )
        except HttpError as exc:
            if endpoint == "accountsByL1Address" and exc.status == 400:
                try:
                    missing = json.loads(exc.body or "{}")
                except (ValueError, TypeError):
                    missing = None
                if isinstance(missing, dict) and missing.get("code") == 21100:
                    return missing
            if endpoint == "positionFunding" and exc.status in {401, 403}:
                raise PositionReadError("Lighter funding requires a valid official read-only token for private accounts.") from None
            raise PositionReadError("Lighter account read failed.") from None
        except Exception:
            raise PositionReadError("Lighter account read failed.") from None
        response = _object(response)
        if response.get("code") != 200:
            if endpoint == "accountsByL1Address" and response.get("code") == 21100:
                return response
            if endpoint == "positionFunding":
                raise PositionReadError("Lighter funding read failed; private accounts require a valid official read-only token.")
            raise PositionReadError("Lighter account read failed.")
        return response

    async def _metadata(self) -> None:
        async with self._metadata_lock:
            if self._markets and time.monotonic() - self._metadata_at < _METADATA_TTL:
                return
            assets_response = await self._get("assetDetails")
            assets: dict[int, dict] = {}
            for raw in _array(assets_response["asset_details"]):
                row = _object(raw)
                index = _integer(row["asset_id"])
                _text(row["symbol"])
                if index in assets or row["margin_mode"] not in {"enabled", "disabled", "priced_only"}:
                    raise PositionReadError("Lighter returned inconsistent asset metadata.")
                assets[index] = row
            if sum(row["symbol"] == "USDC" for row in assets.values()) != 1:
                raise PositionReadError("Lighter settlement asset metadata is unavailable.")
            markets_response = await self._get("orderBookDetails", params={"filter": "all"})
            markets: dict[int, dict] = {}
            for field, kind in (("order_book_details", "perp"), ("spot_order_book_details", "spot")):
                for raw in _array(markets_response[field]):
                    row = _object(raw)
                    index = _integer(row["market_id"])
                    _text(row["symbol"])
                    if index in markets or row["market_type"] != kind:
                        raise PositionReadError("Lighter returned inconsistent market metadata.")
                    # Preserve named contract units rather than guessing a
                    # conversion for any future non-unit perp multiplier.
                    if kind == "perp" and number(row["multiplier"]) != 1:
                        raise PositionReadError("Lighter perp contract multiplier is unsupported.")
                    markets[index] = row
            self._markets, self._assets = markets, assets
            self._metadata_at = time.monotonic()

    async def resolve_accounts(self, wallet: str, *, cursor: str | None = None) -> dict:
        if isinstance(wallet, str) and wallet.isascii() and wallet.isdigit():
            wallet = _index(wallet)
            if cursor is not None:
                raise PositionReadError("An explicit Lighter account index does not use a discovery cursor.")
            return {
                "wallet": wallet, "accounts": [{"address": wallet, "name": f"Account {wallet}"}],
                "next_cursor": None,
                "note": "Tracking this explicit Lighter account index only. Add its wallet address to discover all owned accounts.",
            }
        if not isinstance(wallet, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet):
            raise PositionReadError("Use a Lighter 0x wallet address with 40 hexadecimal characters.")
        wallet = wallet.lower()
        if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 2048):
            raise PositionReadError("Lighter account lookup cursor is invalid.")
        params = {"l1_address": wallet}
        if cursor is not None:
            params["cursor"] = cursor
        response = await self._get("accountsByL1Address", params=params)
        if response["code"] == 21100:
            return {"wallet": wallet, "accounts": [], "next_cursor": None}
        try:
            if _text(response["l1_address"]).lower() != wallet:
                raise ValueError
            accounts = []
            seen = set()
            for raw in _array(response["sub_accounts"]):
                row = _object(raw)
                index = _index(str(_integer(row["index"])))
                status = _integer(row["status"])
                if _text(row["l1_address"]).lower() != wallet or row["code"] not in {0, 200} or status not in {0, 1} or index in seen:
                    raise ValueError
                seen.add(index)
                accounts.append({"address": index, "name": f"Account {index}"})
            next_cursor = response.get("next_cursor") or None
            if next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 2048 or next_cursor == cursor or not accounts):
                raise ValueError
            return {"wallet": wallet, "accounts": accounts, "next_cursor": next_cursor}
        except (KeyError, TypeError, ValueError):
            raise PositionReadError("Lighter returned an inconsistent wallet account list.") from None

    async def fetch_positions(self, address: str) -> PositionSnapshot:
        address = _index(address)
        try:
            await self._metadata()
            response = await self._get("account", params={"by": "index", "value": address})
            accounts = _array(response["accounts"])
            if len(accounts) != 1 or _integer(response["total"]) != 1 or response.get("next_cursor"):
                raise PositionReadError("Lighter did not return one complete account snapshot.")
            account = _object(accounts[0])
            if _integer(account["index"]) != int(address) or account.get("code") not in {0, 200}:
                raise PositionReadError("Lighter returned an inconsistent account snapshot.")
            trading_mode = _integer(account["account_trading_mode"])
            if trading_mode not in {0, 1}:
                raise PositionReadError("Lighter returned an unknown account mode.")
            collateral = ("USDC",) if trading_mode == 0 else tuple(sorted(
                row["symbol"] for row in self._assets.values() if row["margin_mode"] == "enabled"
            ))
            snapshot = PositionSnapshot()
            seen = set()
            for raw in _array(account["positions"]):
                row = _object(raw)
                market_id = _integer(row["market_id"])
                if market_id in seen:
                    raise PositionReadError("Lighter returned duplicate positions.")
                seen.add(market_id)
                size = number(row["position"])
                if size < 0:
                    raise PositionReadError("Lighter returned a negative unsigned position size.")
                if not size:
                    continue
                market = self._markets[market_id]
                if market["market_type"] != "perp" or _text(row["symbol"]) != market["symbol"]:
                    raise PositionReadError("Lighter returned inconsistent perpetual position metadata.")
                sign = row["sign"]
                if isinstance(sign, bool) or sign not in {-1, 1}:
                    raise PositionReadError("Lighter returned an invalid position direction.")
                value = number(row["position_value"])
                if value < 0:
                    raise PositionReadError("Lighter returned a negative position value.")
                snapshot.positions.append(Position(
                    symbol=market["symbol"], quantity=size * sign,
                    quote_currency="USDC", settlement_currency="USDC",
                    collateral_currencies=collateral, mark_price=value / size,
                    entry_price=number(row["avg_entry_price"]),
                    unrealized_pnl=number(row["unrealized_pnl"]),
                ))
            seen_assets = set()
            for raw in _array(account["assets"]):
                row = _object(raw)
                asset_id = _integer(row["asset_id"])
                if asset_id in seen_assets or _text(row["symbol"]) != self._assets[asset_id]["symbol"]:
                    raise PositionReadError("Lighter returned inconsistent spot asset metadata.")
                seen_assets.add(asset_id)
                amount = number(row["balance"])
                if amount:
                    snapshot.balances.append(CurrencyAmount(row["symbol"], amount))
            # The venue's own account value is preserved in USDC accounting;
            # collateral and asset balances are not added again to this value.
            snapshot.equity.append(CurrencyAmount("USDC", number(account["total_asset_value"])))
            snapshot.notes.append("Spot balances are separate from perpetual positions; explicit subaccounts must be tracked separately.")
            if trading_mode == 1:
                snapshot.notes.append("Collateral metadata lists enabled margin assets; it does not imply every listed asset is pledged.")
            return snapshot
        except PositionReadError:
            raise
        except Exception:
            raise PositionReadError("Lighter returned malformed or incomplete account data.") from None

    async def fetch_funding(
        self, address: str, since: datetime, until: datetime, *,
        cursor: dict | None = None, token: str | None = None,
    ) -> FundingPage:
        address = _index(address)
        if token is not None and (not isinstance(token, str) or not re.fullmatch(
            r"ro:[0-9]+:(?:single|all):[0-9]+:[0-9a-fA-F]+", token
        )):
            raise PositionReadError("Lighter requires an official ro: read-only token, not a trading API key.")
        try:
            await self._metadata()
            if cursor is None:
                start, end = _seconds(since), _seconds(until)
                page_cursor, prior_cursors = None, []
            else:
                cursor = _object(cursor)
                if cursor["version"] != 1 or cursor["address"] != address:
                    raise PositionReadError("Lighter funding cursor is invalid.")
                start, end = _integer(cursor["since"]), _integer(cursor["until"])
                page_cursor = _text(cursor["next"])
                prior_cursors = [_text(value) for value in _array(cursor["seen_cursors"])]
            if not 0 <= start <= end:
                raise PositionReadError("Lighter funding date range is invalid.")
            params = {
                "account_index": address, "limit": "100", "side": "all",
                "start_timestamp": str(start), "end_timestamp": str(end),
            }
            if page_cursor is not None:
                params["cursor"] = page_cursor
            response = await self._get("positionFunding", params=params, token=token)
            rows = _array(response["position_fundings"])
            if len(rows) > 100:
                raise PositionReadError("Lighter returned an oversized funding page.")
            events: dict[str, FundingEvent] = {}
            for raw in rows:
                row = _object(raw)
                ts = _integer(row["timestamp"])
                if not start <= ts <= end:
                    raise PositionReadError("Lighter returned funding outside the requested range.")
                market_id = _integer(row["market_id"])
                market = self._markets[market_id]
                if market["market_type"] != "perp":
                    raise PositionReadError("Lighter returned spot funding data.")
                funding_id = _integer(row["funding_id"])
                event_id = f"{market_id}:{funding_id}"
                event = FundingEvent(
                    event_id=event_id, ts=datetime.fromtimestamp(ts, timezone.utc),
                    symbol=market["symbol"], amount=number(row["change"]), currency="USDC",
                )
                if event_id in events and events[event_id] != event:
                    raise PositionReadError("Lighter returned inconsistent funding records.")
                events[event_id] = event
            next_cursor = response.get("next_cursor", "")
            if not isinstance(next_cursor, str):
                raise PositionReadError("Lighter returned a malformed funding continuation.")
            if "next_cursor" not in response and len(rows) == 100:
                raise PositionReadError("Lighter omitted a full funding page's continuation.")
            continuation = None
            if next_cursor:
                if not rows or next_cursor == page_cursor or next_cursor in prior_cursors:
                    raise PositionReadError("Lighter funding pagination did not advance.")
                continuation = {
                    "version": 1, "address": address, "since": start, "until": end,
                    "next": next_cursor,
                    "seen_cursors": [*prior_cursors, *([page_cursor] if page_cursor else [])],
                }
            complete = continuation is None
            return FundingPage(
                events=list(events.values()), cursor=continuation, complete=complete,
                coverage_since=datetime.fromtimestamp(start, timezone.utc) if complete else None,
                coverage_note="Settled positionFunding ledger; historical availability is limited to what the venue API returns."
                if complete else "Funding history pagination is still in progress.",
            )
        except PositionReadError:
            raise
        except Exception:
            raise PositionReadError("Lighter returned malformed funding data or cursor.") from None

    async def aclose(self) -> None:
        await self.http.aclose()
