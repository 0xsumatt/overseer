from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any

from core.positions import (
    CurrencyAmount, FundingEvent, FundingPage, Position, PositionReadError,
    PositionSnapshot, number,
)
from data_collection.http import HttpClient
from data_collection.ratelimit import RateLimiter

_INFO = "https://api.hyperliquid.xyz/info"
_METADATA_TTL = 1800


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise PositionReadError("Hyperliquid returned malformed account data.")
    return value


def _array(value: Any) -> list:
    if not isinstance(value, list):
        raise PositionReadError("Hyperliquid returned malformed account data.")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PositionReadError("Hyperliquid returned malformed market metadata.")
    return value


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PositionReadError("Hyperliquid returned malformed account data.")
    return value


def _address(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise PositionReadError("Hyperliquid requires a public 0x account address.")
    return value.lower()


def _milliseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PositionReadError("Funding dates must include a timezone.")
    return int(value.timestamp() * 1000)


class HyperliquidPositions:
    """Native and every HIP-3 DEX, using only read-only /info requests.

    Schemas: Hyperliquid API info-endpoint/{perpetuals,spot}. Monetary fields
    on HIP-3 are in the DEX collateral token, even legacy fields named usdc.
    Contract names and sizes are preserved; no multiplier is inferred.
    """

    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http if http is not None else HttpClient(
            limiter=RateLimiter.per_minute(60, burst=2)
        )
        self._metadata_at = 0.0
        self._tokens: dict[int, str] = {}
        self._scopes: dict[str, tuple[str, set[str]]] = {}
        self._metadata_lock = asyncio.Lock()
        self._portfolio_at = 0.0
        self._portfolio_collateral: tuple[str, ...] = ()

    async def _info(self, kind: str, **params: Any) -> Any:
        try:
            return await self.http.post_json(_INFO, json={"type": kind, **params})
        except Exception:
            raise PositionReadError("Hyperliquid account read failed.") from None

    async def _metadata(self) -> None:
        async with self._metadata_lock:
            if self._scopes and time.monotonic() - self._metadata_at < _METADATA_TTL:
                return
            spot = _object(await self._info("spotMeta"))
            tokens: dict[int, str] = {}
            for raw in _array(spot["tokens"]):
                row = _object(raw)
                index = _integer(row["index"])
                if index in tokens:
                    raise PositionReadError("Hyperliquid returned duplicate token metadata.")
                tokens[index] = _text(row["name"])
            dexes = _array(await self._info("perpDexs"))
            if not dexes or dexes[0] is not None:
                raise PositionReadError("Hyperliquid did not return the full DEX universe.")
            names = [""]
            for raw in dexes[1:]:
                name = _text(_object(raw)["name"])
                if name in names:
                    raise PositionReadError("Hyperliquid returned duplicate DEX metadata.")
                names.append(name)
            scopes: dict[str, tuple[str, set[str]]] = {}
            for dex in names:
                # metaAndAssetCtxs explicitly supplies collateralToken for native
                # and HIP-3 markets, unlike older examples of the meta endpoint.
                meta_contexts = _array(await self._info("metaAndAssetCtxs", dex=dex))
                if len(meta_contexts) != 2:
                    raise PositionReadError("Hyperliquid returned malformed DEX metadata.")
                meta = _object(meta_contexts[0])
                collateral = tokens[_integer(meta["collateralToken"])]
                universe = _array(meta["universe"])
                if len(_array(meta_contexts[1])) != len(universe):
                    raise PositionReadError("Hyperliquid returned incomplete DEX metadata.")
                symbols = {_text(_object(row)["name"]) for row in universe}
                if len(symbols) != len(universe):
                    raise PositionReadError("Hyperliquid returned duplicate market metadata.")
                if any((symbol.split(":", 1)[0] if ":" in symbol else "") != dex for symbol in symbols):
                    raise PositionReadError("Hyperliquid returned inconsistent DEX metadata.")
                scopes[dex] = (collateral, symbols)
            self._tokens, self._scopes = tokens, scopes
            self._metadata_at = time.monotonic()

    async def _portfolio_tokens(self) -> tuple[str, ...]:
        if time.monotonic() - self._portfolio_at >= _METADATA_TTL:
            currencies = set()
            for raw in _array(await self._info("allBorrowLendReserveStates")):
                row = _array(raw)
                if len(row) != 2:
                    raise PositionReadError("Hyperliquid returned malformed collateral metadata.")
                if number(_object(row[1])["ltv"]) > 0:
                    currencies.add(self._tokens[_integer(row[0])])
            self._portfolio_collateral = tuple(sorted(currencies))
            self._portfolio_at = time.monotonic()
        return self._portfolio_collateral

    async def resolve_accounts(self, wallet: str, *, cursor: str | None = None) -> dict:
        wallet = _address(wallet)
        if cursor is not None:
            raise PositionReadError("Hyperliquid account discovery does not use a cursor.")
        rows = await self._info("subAccounts", user=wallet)
        accounts = [{"address": wallet, "name": "Main account"}]
        seen = {wallet}
        try:
            for raw in _array([] if rows is None else rows):
                row = _object(raw)
                address = _address(row["subAccountUser"])
                if _address(row["master"]) != wallet or address in seen:
                    raise ValueError
                name = row.get("name")
                if name is not None and not isinstance(name, str):
                    raise ValueError
                name = "".join(char for char in (name or "") if ord(char) >= 32 and ord(char) != 127).strip()[:80]
                accounts.append({"address": address, "name": name or f"Subaccount {len(accounts)}"})
                seen.add(address)
        except (KeyError, TypeError, ValueError):
            raise PositionReadError("Hyperliquid returned an inconsistent owned subaccount list.") from None
        return {"wallet": wallet, "accounts": accounts, "next_cursor": None}

    async def fetch_positions(self, address: str) -> PositionSnapshot:
        address = _address(address)
        try:
            await self._metadata()
            mode = await self._info("userAbstraction", user=address)
            if mode not in {"unifiedAccount", "portfolioMargin", "disabled", "default", "dexAbstraction"}:
                raise PositionReadError("Hyperliquid returned an unknown account mode.")
            portfolio = await self._portfolio_tokens() if mode == "portfolioMargin" else ()
            snapshot = PositionSnapshot()
            for dex, (currency, symbols) in self._scopes.items():
                state = _object(await self._info("clearinghouseState", user=address, dex=dex))
                rows = _array(state["assetPositions"])
                # Per-DEX equity is not meaningful in unified/portfolio mode.
                account_value = number(_object(state["marginSummary"])["accountValue"])
                if mode in {"disabled", "default"}:
                    snapshot.equity.append(CurrencyAmount(currency, account_value))
                seen = set()
                for raw in rows:
                    row = _object(_object(raw)["position"])
                    symbol = _text(row["coin"])
                    if symbol not in symbols or symbol in seen:
                        raise PositionReadError("Hyperliquid returned an unknown or duplicate position.")
                    seen.add(symbol)
                    quantity = number(row["szi"])
                    if not quantity:
                        continue
                    value = number(row["positionValue"])
                    if value < 0:
                        raise PositionReadError("Hyperliquid returned a negative position value.")
                    snapshot.positions.append(Position(
                        symbol=symbol, quantity=quantity, quote_currency=currency,
                        settlement_currency=currency,
                        collateral_currencies=tuple(sorted({currency, *portfolio})),
                        mark_price=value / abs(quantity),
                        entry_price=number(row["entryPx"]) if row.get("entryPx") is not None else None,
                        unrealized_pnl=number(row["unrealizedPnl"]), scope=dex or "main",
                    ))
            spot = _object(await self._info("spotClearinghouseState", user=address))
            seen_tokens = set()
            for raw in _array(spot["balances"]):
                row = _object(raw)
                index = _integer(row["token"])
                if index in seen_tokens or _text(row["coin"]) != self._tokens[index]:
                    raise PositionReadError("Hyperliquid returned inconsistent spot balances.")
                seen_tokens.add(index)
                amount = number(row["total"])
                if amount:
                    snapshot.balances.append(CurrencyAmount(self._tokens[index], amount))
            snapshot.notes.append("Spot/token holdings are separate from perpetual positions; explicit subaccounts must be tracked separately.")
            if mode not in {"disabled", "default"}:
                snapshot.notes.append("Shared-balance account mode: per-DEX equity is omitted to avoid double counting.")
            return snapshot
        except PositionReadError:
            raise
        except Exception:
            raise PositionReadError("Hyperliquid returned malformed or incomplete account data.") from None

    async def fetch_funding(
        self, address: str, since: datetime, until: datetime, *,
        cursor: dict | None = None, token: str | None = None,
    ) -> FundingPage:
        address = _address(address)
        try:
            await self._metadata()
            if cursor is None:
                start, end = _milliseconds(since), _milliseconds(until)
                origin, previous = start, set()
            else:
                cursor = _object(cursor)
                if cursor["version"] != 1 or cursor["address"] != address:
                    raise PositionReadError("Hyperliquid funding cursor is invalid.")
                start, end = _integer(cursor["start"]), _integer(cursor["end"])
                origin = _integer(cursor["since"])
                previous = {_text(value) for value in _array(cursor["boundary_ids"])}
            if not 0 <= origin <= start <= end:
                raise PositionReadError("Hyperliquid funding date range is invalid.")
            # userFunding covers every DEX; the coin namespace identifies scope.
            rows = _array(await self._info("userFunding", user=address, startTime=start, endTime=end))
            events: dict[str, FundingEvent] = {}
            boundary_ids: set[str] = set()
            last = start
            for raw in rows:
                row = _object(raw)
                ts = _integer(row["time"])
                if not start <= ts <= end or ts < last:
                    raise PositionReadError("Hyperliquid funding history is not ordered within its requested range.")
                if ts > last:
                    boundary_ids.clear()
                last = ts
                delta = _object(row["delta"])
                if delta["type"] != "funding":
                    raise PositionReadError("Hyperliquid returned a non-funding ledger record.")
                symbol = _text(delta["coin"])
                dex = symbol.split(":", 1)[0] if ":" in symbol else ""
                currency, symbols = self._scopes[dex]
                if symbol not in symbols:
                    raise PositionReadError("Hyperliquid funding market metadata is unavailable.")
                event_id = f"{_text(row['hash'])}:{ts}:{symbol}"
                boundary_ids.add(event_id)
                event = FundingEvent(
                    event_id=event_id, ts=datetime.fromtimestamp(ts / 1000, timezone.utc),
                    symbol=symbol, amount=number(delta["usdc"]), currency=currency,
                    scope=dex or "main",
                )
                if event_id in events and events[event_id] != event:
                    raise PositionReadError("Hyperliquid returned inconsistent funding records.")
                if ts != start or event_id not in previous:
                    events[event_id] = event
            complete = len(rows) < 500
            continuation = None
            if not complete:
                # Inclusive timestamp pagination retains the boundary for ties.
                if cursor is not None and last == start and boundary_ids <= previous:
                    raise PositionReadError("Hyperliquid funding pagination did not advance.")
                continuation = {
                    "version": 1, "address": address, "since": origin, "end": end,
                    "start": last, "boundary_ids": sorted(boundary_ids | (previous if last == start else set())),
                }
            return FundingPage(
                events=list(events.values()), cursor=continuation, complete=complete,
                coverage_since=datetime.fromtimestamp(origin / 1000, timezone.utc) if complete else None,
                coverage_note="Settled userFunding ledger across native and HIP-3 DEXs; historical availability is limited to what the venue API returns."
                if complete else "Funding history pagination is still in progress.",
            )
        except PositionReadError:
            raise
        except Exception:
            raise PositionReadError("Hyperliquid returned malformed funding data or cursor.") from None

    async def aclose(self) -> None:
        await self.http.aclose()
