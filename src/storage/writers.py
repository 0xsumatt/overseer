from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

import asyncpg

from core.enums import Exchange, MarketType, Timeframe
from core.models import OHLCV, FundingRate, LiquiditySnapshot, Trade

# (db column, postgres array cast, value extractor)
_Col = tuple[str, str, Callable[[Any], Any]]

_OHLCV_COLS: tuple[_Col, ...] = (
    ("exchange",    "text",        lambda r: r.exchange.value),
    ("market_type", "text",        lambda r: r.market_type.value),
    ("symbol",      "text",        lambda r: r.symbol),
    ("interval",    "text",        lambda r: r.interval.value),
    ("ts",          "timestamptz", lambda r: r.ts),
    ("open",        "numeric",     lambda r: r.open),
    ("high",        "numeric",     lambda r: r.high),
    ("low",         "numeric",     lambda r: r.low),
    ("close",       "numeric",     lambda r: r.close),
    ("volume",      "numeric",     lambda r: r.volume),
)
_OHLCV_CONFLICT = ("exchange", "market_type", "symbol", "interval", "ts")

_TRADE_COLS: tuple[_Col, ...] = (
    ("exchange",       "text",        lambda r: r.exchange.value),
    ("market_type",    "text",        lambda r: r.market_type.value),
    ("symbol",         "text",        lambda r: r.symbol),
    ("trade_id",       "text",        lambda r: r.trade_id),
    ("ts",             "timestamptz", lambda r: r.ts),
    ("price",          "numeric",     lambda r: r.price),
    ("amount",         "numeric",     lambda r: r.amount),
    ("side",           "text",        lambda r: r.side.value),
    ("wallet_address", "text",        lambda r: r.wallet_address),
)
# ts joins the conflict key because a hypertable's unique index must contain the
# partitioning column; dedup-safe since a trade_id maps to exactly one ts.
_TRADE_CONFLICT = ("exchange", "market_type", "symbol", "trade_id", "ts")



_FUNDING_COLS: tuple[_Col, ...] = (
    ("exchange",       "text",        lambda r: r.exchange.value),
    ("symbol",         "text",        lambda r: r.symbol),
    ("ts",             "timestamptz", lambda r: r.ts),
    ("rate",           "numeric",     lambda r: r.rate),
    ("interval_hours", "int4",        lambda r: r.interval_hours),
)
_FUNDING_CONFLICT = ("exchange", "symbol", "ts")

_LIQ_COLS: tuple[_Col, ...] = (
    ("exchange",      "text",        lambda r: r.exchange.value),
    ("symbol",        "text",        lambda r: r.symbol),
    ("ts",            "timestamptz", lambda r: r.ts),
    ("open_interest", "numeric",     lambda r: r.open_interest),
    ("volume_24h",    "numeric",     lambda r: r.volume_24h),
    ("mark_price",    "numeric",     lambda r: r.mark_price),
)
_LIQ_CONFLICT = ("exchange", "symbol", "ts")


class Storage:
    """Async write access to the Timescale tables. One per process."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn, server_settings={"timezone": "UTC"}
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Storage.connect() has not been called")
        return self._pool

    async def __aenter__(self) -> "Storage":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # -- writes -------------------------------------------------------------------

    async def write_ohlcv(self, records: Sequence[OHLCV]) -> int:
        return await self._bulk_upsert(
            "ohlcv", _OHLCV_COLS, _OHLCV_CONFLICT, records, update=True
        )

    async def write_trades(self, records: Sequence[Trade]) -> int:
        return await self._bulk_upsert("trades", _TRADE_COLS, _TRADE_CONFLICT, records)

    async def _bulk_upsert(
        self,
        table: str,
        cols: tuple[_Col, ...],
        conflict: tuple[str, ...],
        records: Sequence[Any],
        *,
        update: bool = False,
    ) -> int:
        if not records:
            return 0
        col_names = ", ".join(c[0] for c in cols)
        unnest = ", ".join(f"${i + 1}::{c[1]}[]" for i, c in enumerate(cols))
        on_conflict = ", ".join(conflict)
        if update:
            sets = ", ".join(
                f"{c[0]} = EXCLUDED.{c[0]}" for c in cols if c[0] not in conflict
            )
            # xmax = 0 only on freshly inserted rows, so "new" still means new
            # even though conflicting rows are rewritten.
            action = f"DO UPDATE SET {sets} RETURNING (xmax = 0) AS is_new"
        else:
            action = "DO NOTHING RETURNING TRUE AS is_new"
        sql = (
            f"INSERT INTO {table} ({col_names}) "
            f"SELECT * FROM unnest({unnest}) "
            f"ON CONFLICT ({on_conflict}) {action}"
        )
        # one parallel array per column, expanded row-wise by unnest()
        arrays = [[extract(r) for r in records] for _, _, extract in cols]
        rows = await self.pool.fetch(sql, *arrays)
        return sum(1 for r in rows if r["is_new"])

    # -- resume points (the scheduler's "where did I leave off") ------------------

    async def latest_ohlcv_ts(
        self,
        exchange: Exchange,
        market_type: MarketType,
        symbol: str,
        interval: Timeframe,
    ) -> datetime | None:
        return await self.pool.fetchval(
            "SELECT max(ts) FROM ohlcv "
            "WHERE exchange=$1 AND market_type=$2 AND symbol=$3 AND interval=$4",
            exchange.value, market_type.value, symbol, interval.value,
        )

    async def latest_trade_ts(
        self, exchange: Exchange, market_type: MarketType, symbol: str
    ) -> datetime | None:
        return await self.pool.fetchval(
            "SELECT max(ts) FROM trades "
            "WHERE exchange=$1 AND market_type=$2 AND symbol=$3",
            exchange.value, market_type.value, symbol,
        )

    async def latest_fill_ts(
        self, exchange: Exchange, wallet_address: str
    ) -> datetime | None:
        """Resume point for a tracked address's fills (spans all its symbols).
        Hits the partial wallet index, since it filters on a non-null wallet."""
        return await self.pool.fetchval(
            "SELECT max(ts) FROM trades WHERE exchange=$1 AND wallet_address=$2",
            exchange.value, wallet_address,
        )


    async def write_funding(self, records: Sequence[FundingRate]) -> int:
        return await self._bulk_upsert("funding_rates", _FUNDING_COLS, _FUNDING_CONFLICT, records)

    async def write_liquidity(self, records: Sequence[LiquiditySnapshot]) -> int:
        return await self._bulk_upsert("liquidity", _LIQ_COLS, _LIQ_CONFLICT, records)

    async def latest_funding_ts(self, exchange: Exchange, symbol: str) -> datetime | None:
        return await self.pool.fetchval(
            "SELECT max(ts) FROM funding_rates WHERE exchange=$1 AND symbol=$2",
            exchange.value, symbol,
        )

    # -- heartbeat (job health) ---------------------------------------------------

    async def record_job_run(
        self,
        job_id: str,
        status: str,            # 'ok' | 'fail'
        fetched: int,
        new_rows: int,
        error: str | None,
        ran_at: datetime,
    ) -> None:
        """Upsert the current state of a job. last_success_at is advanced only on
        success and preserved through failures, so the health view can show both
        'currently failing' and 'last worked at …'."""
        await self.pool.execute(
            """
            INSERT INTO job_runs (job_id, last_run_at, last_status, fetched,
                                  new_rows, last_error, last_success_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6,
                    CASE WHEN $3 = 'ok' THEN $2::timestamptz
                         ELSE NULL::timestamptz END, now())
            ON CONFLICT (job_id) DO UPDATE SET
                last_run_at     = EXCLUDED.last_run_at,
                last_status     = EXCLUDED.last_status,
                fetched         = EXCLUDED.fetched,
                new_rows        = EXCLUDED.new_rows,
                last_error      = EXCLUDED.last_error,
                last_success_at = CASE WHEN EXCLUDED.last_status = 'ok'
                                       THEN EXCLUDED.last_run_at
                                       ELSE job_runs.last_success_at END,
                updated_at      = now()
            """,
            job_id, ran_at, status, fetched, new_rows, error,
        )

    async def all_job_runs(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT job_id, last_run_at, last_success_at, last_status, "
            "fetched, new_rows, last_error FROM job_runs ORDER BY job_id"
        )