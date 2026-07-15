from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

Row = dict[str, Any]


class ReadStorage:
    """Pooled, sync read access. Create one per web process; it's thread-safe."""

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 8) -> None:
        self._pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row},
            open=True,
        )

    def close(self) -> None:
        self._pool.close()

    def _fetch(self, sql: str, params: tuple = ()) -> list[Row]:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    # -- candles (feeds the charts) -------------------------------------------

    def candles(
        self,
        exchange: str,
        market_type: str,
        symbol: str,
        interval: str,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
    ) -> list[Row]:
        """Bars for one series, ascending by time (what charting libs expect).

        The LIMIT applies to the *most recent* bars (inner DESC), then reorders
        ascending — so "last 500 bars" is the natural call shape.
        """
        since = since or datetime(1970, 1, 1, tzinfo=timezone.utc)
        until = until or datetime.now(timezone.utc)
        return self._fetch(
            """
            SELECT ts, open, high, low, close, volume FROM (
                SELECT ts, open, high, low, close, volume
                FROM ohlcv
                WHERE exchange=%s AND market_type=%s AND symbol=%s
                  AND interval=%s AND ts >= %s AND ts <= %s
                ORDER BY ts DESC
                LIMIT %s
            ) recent ORDER BY ts ASC
            """,
            (exchange, market_type, symbol, interval, since, until, limit),
        )

    def series_list(self) -> list[Row]:
        """Every (exchange, market_type, symbol, interval) we hold, with bar
        counts and coverage — drives dropdowns and the health page."""
        return self._fetch(
            """
            SELECT exchange, market_type, symbol, interval,
                   count(*) AS bars, min(ts) AS first_ts, max(ts) AS last_ts
            FROM ohlcv
            GROUP BY 1, 2, 3, 4
            ORDER BY 1, 2, 3, 4
            """
        )

    # -- spot-perp basis (the analysis this project exists for) ----------------

    def basis(
        self,
        spot_exchange: str,
        spot_symbol: str,
        perp_exchange: str,
        perp_symbol: str,
        interval: str,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[Row]:
        """Join spot and perp closes on the bar timestamp.

        basis = perp - spot; basis_pct = basis / spot * 100.
        Parameterised by venue+symbol on each leg, so it covers same-venue
        (binance spot vs binance perp, same symbol) AND cross-venue
        (binance BTC/USDT spot vs hyperliquid BTC perp) with one query.
        INNER JOIN: a bar missing on either leg is simply absent, not null.
        """
        since = since or (datetime.now(timezone.utc) - timedelta(days=7))
        return self._fetch(
            """
            SELECT ts, spot_close, perp_close,
                   perp_close - spot_close                              AS basis,
                   (perp_close - spot_close) / spot_close * 100         AS basis_pct
            FROM (
                SELECT s.ts, s.close AS spot_close, p.close AS perp_close
                FROM ohlcv s
                JOIN ohlcv p
                  ON p.ts = s.ts AND p.interval = s.interval
                 AND p.exchange=%s AND p.market_type='perp' AND p.symbol=%s
                WHERE s.exchange=%s AND s.market_type='spot' AND s.symbol=%s
                  AND s.interval=%s AND s.ts >= %s
                ORDER BY s.ts DESC
                LIMIT %s
            ) joined ORDER BY ts ASC
            """,
            (perp_exchange, perp_symbol, spot_exchange, spot_symbol,
             interval, since, limit),
        )


    # -- funding table (funding vs liquidity, per coin per venue) ---------------

    def funding_table(self) -> list[Row]:
        """One row per (venue, perp): the latest settled funding — annualized so
        hourly (HL) and 8h/4h (Binance) rates are comparable — with the latest
        liquidity snapshot alongside.

        Rows come back keyed by (exchange, symbol); grouping venue rows under a
        canonical asset is the API layer's job via core.symbols.SymbolRegistry
        (declared mapping — a string heuristic would mis-key e.g. "BTC-USD").

        apr_pct = rate * (8760 / interval_hours) * 100
        oi_notional = open_interest (base units) * mark_price
        """
        return self._fetch(
            """
            WITH latest_funding AS (
                SELECT DISTINCT ON (exchange, symbol)
                       exchange, symbol, ts, rate, interval_hours
                FROM funding_rates
                ORDER BY exchange, symbol, ts DESC
            ),
            latest_liq AS (
                SELECT DISTINCT ON (exchange, symbol)
                       exchange, symbol, ts, open_interest, volume_24h, mark_price
                FROM liquidity
                ORDER BY exchange, symbol, ts DESC
            )
            SELECT
                f.exchange, f.symbol,
                f.ts                                            AS funding_ts,
                f.rate, f.interval_hours,
                f.rate * (8760.0 / f.interval_hours) * 100      AS apr_pct,
                l.open_interest,
                l.open_interest * l.mark_price                  AS oi_notional,
                l.volume_24h, l.mark_price,
                l.ts                                            AS liq_ts
            FROM latest_funding f
            LEFT JOIN latest_liq l
              ON l.exchange = f.exchange AND l.symbol = f.symbol
            ORDER BY f.symbol, f.exchange
            """
        )

    # -- health (the internal soak-monitoring page) -----------------------------

    def freshness(self) -> list[Row]:
        """Age of the newest bar per series — the leading is-it-alive signal.
        stale_factor = age / interval length; > ~3 means the series has stopped."""
        return self._fetch(
            """
            SELECT exchange, market_type, symbol, interval,
                   max(ts)                                   AS last_ts,
                   now() - max(ts)                           AS age,
                   EXTRACT(EPOCH FROM (now() - max(ts)))
                     / EXTRACT(EPOCH FROM interval::interval) AS stale_factor
            FROM ohlcv
            GROUP BY 1, 2, 3, 4
            ORDER BY stale_factor DESC
            """
        )

    def job_health(self) -> list[Row]:
        """Current heartbeat state of every scheduled job (job_runs table)."""
        return self._fetch(
            """
            SELECT job_id, last_run_at, last_success_at, last_status,
                   fetched, new_rows, last_error,
                   now() - last_success_at AS since_success
            FROM job_runs
            ORDER BY (last_status = 'fail') DESC, job_id
            """
        )

    def fills_summary(self, wallet_address: str) -> list[Row]:
        """Per-symbol/side rollup of a tracked address's fills (e.g. HLP)."""
        return self._fetch(
            """
            SELECT symbol, market_type, side,
                   count(*) AS fills, sum(amount) AS total_amount,
                   min(ts) AS first_ts, max(ts) AS last_ts
            FROM trades
            WHERE wallet_address = %s
            GROUP BY 1, 2, 3
            ORDER BY fills DESC
            """,
            (wallet_address,),
        )