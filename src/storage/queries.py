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
        oi_delta_pct = OI now vs the closest snapshot >= 24h old (liquidity
        polls every 5min, so "ts = now - 24h" exactly rarely exists) — building
        OI + rich funding is the squeeze setup the dislocation alerts can't see.
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
            ),
            liq_24h_ago AS (
                SELECT DISTINCT ON (exchange, symbol)
                       exchange, symbol, open_interest AS oi_24h_ago
                FROM liquidity
                WHERE ts <= now() - interval '24 hours'
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
                l.ts                                            AS liq_ts,
                CASE WHEN a.oi_24h_ago IS NOT NULL AND a.oi_24h_ago != 0
                     THEN (l.open_interest - a.oi_24h_ago) / a.oi_24h_ago * 100
                     ELSE NULL END                               AS oi_delta_pct
            FROM latest_funding f
            LEFT JOIN latest_liq l
              ON l.exchange = f.exchange AND l.symbol = f.symbol
            LEFT JOIN liq_24h_ago a
              ON a.exchange = f.exchange AND a.symbol = f.symbol
            ORDER BY f.symbol, f.exchange
            """
        )




    def funding_series(self, exchange: str, symbol: str,
                       hours: int = 48, limit: int = 2000) -> list[Row]:
        """Settled funding history for one perp — the carry overlay on the
        basis chart. apr_pct annualizes across venue intervals (1h/4h/8h)."""
        return self._fetch(
            """
            SELECT ts, rate, interval_hours,
                   (rate * (8760.0 / interval_hours) * 100)::numeric AS apr_pct
            FROM funding_rates
            WHERE exchange = %s AND symbol = %s
              AND ts >= now() - make_interval(hours => %s)
            ORDER BY ts
            LIMIT %s
            """,
            (exchange, symbol, hours, limit),
        )

    def fills_pulse(self) -> list[Row]:
        """24h activity per tracked account — the MM pulse widget."""
        return self._fetch(
            """
            SELECT wallet_address,
                   count(*)                                   AS fills_24h,
                   count(*) FILTER (WHERE side = 'buy')        AS buys,
                   count(*) FILTER (WHERE side = 'sell')       AS sells,
                   max(ts)                                     AS last_ts
            FROM trades
            WHERE wallet_address IS NOT NULL
              AND ts >= now() - interval '24 hours'
            GROUP BY 1
            """
        )

    # -- venue-wide volume (daily sweep) ----------------------------------------

    def venue_volume(self, days: int = 30) -> dict:
        """Latest per-venue totals + the daily CEX-share series.
        cex = binance + bybit; share is of TRACKED venues, not the whole market."""
        latest = self._fetch(
            """
            SELECT DISTINCT ON (exchange)
                   exchange, ts, volume_total, volume_spot, volume_perp
            FROM venue_volume
            ORDER BY exchange, ts DESC
            """
        )
        series = self._fetch(
            """
            SELECT ts,
                   sum(volume_total) FILTER (WHERE exchange IN ('binance','bybit'))
                     / NULLIF(sum(volume_total), 0) * 100    AS cex_share_pct,
                   sum(volume_total)                         AS total
            FROM venue_volume
            WHERE ts >= now() - make_interval(days => %s)
            GROUP BY ts ORDER BY ts
            """,
            (days,),
        )
        return {"latest": latest, "series": series}

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

    def ingest_freshness(self) -> Row | None:
        """Age of the newest bar anywhere — the 'is ingest alive at all' signal
        behind the public-page stale banner. Per-series staleness (one venue
        quietly dead) stays the health page's job via freshness()."""
        rows = self._fetch(
            "SELECT max(ts) AS last_ts, "
            "EXTRACT(EPOCH FROM (now() - max(ts))) AS age_seconds FROM ohlcv"
        )
        return rows[0] if rows and rows[0]["last_ts"] is not None else None

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

    def wallet_symbols(self, addresses: list[str]) -> list[Row]:
        """Symbols any tracked wallet has traded, most active first — drives
        the wallets page coin selector."""
        return self._fetch(
            "SELECT symbol, count(*) AS fills FROM trades "
            "WHERE wallet_address = ANY(%s) GROUP BY 1 ORDER BY fills DESC",
            (addresses,),
        )

    def wallet_flows(
        self, symbol: str, addresses: list[str], since: datetime
    ) -> list[Row]:
        """5-minute net signed flow (buys − sells, base units) per wallet for
        one symbol. The wallets page cumulates client-side, so a bucket with no
        fills simply doesn't emit a row."""
        return self._fetch(
            """
            SELECT wallet_address, time_bucket('5 minutes', ts) AS bucket,
                   sum(CASE WHEN side = 'buy' THEN amount ELSE -amount END) AS net
            FROM trades
            WHERE symbol = %s AND wallet_address = ANY(%s) AND ts >= %s
            GROUP BY 1, 2 ORDER BY 2
            """,
            (symbol, addresses, since),
        )

    def wallet_volume_24h(self, addresses: list[str]) -> list[Row]:
        """Quote notional each tracked wallet filled in the last 24h — the
        numerator for the wallet-share meters."""
        return self._fetch(
            "SELECT wallet_address, sum(price * amount) AS notional, count(*) AS fills "
            "FROM trades "
            "WHERE wallet_address = ANY(%s) AND ts >= now() - interval '24 hours' "
            "GROUP BY 1",
            (addresses,),
        )

    def venue_volume_latest(self, exchanges: list[str]) -> dict[str, Any]:
        """Latest daily-sweep total per exchange, keyed — the wallet-share
        denominator. A venue with no sweep yet is simply absent from the dict
        rather than raising, so the meter reads '—' instead of erroring."""
        rows = self._fetch(
            "SELECT DISTINCT ON (exchange) exchange, volume_total FROM venue_volume "
            "WHERE exchange = ANY(%s) ORDER BY exchange, ts DESC",
            (exchanges,),
        )
        return {r["exchange"]: r["volume_total"] for r in rows}

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