from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

Row = dict[str, Any]


class ReadStorage:

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

    def orderbook_spreads(
        self,
        market_type: str,
        symbols: list[str],
        exchanges: list[str],
        since: datetime,
        bucket_minutes: int,
    ) -> list[Row]:
        """Bucketed executable bid/ask spread history for one canonical asset."""
        if not symbols or not exchanges:
            return []
        return self._fetch(
            """
            SELECT exchange, market_type, symbol,
                   date_bin(%s::interval, ts, TIMESTAMPTZ '1970-01-01') AS ts,
                   avg((ask_price - bid_price)
                       / NULLIF((ask_price + bid_price) / 2, 0) * 10000)
                       AS spread_bps,
                   avg((ask_price + bid_price) / 2) AS midpoint,
                   avg(bid_price) AS bid_price,
                   avg(ask_price) AS ask_price,
                   avg(bid_size) AS bid_size,
                   avg(ask_size) AS ask_size
            FROM orderbook_snapshots
            WHERE market_type=%s
              AND symbol = ANY(%s)
              AND exchange = ANY(%s)
              AND ts >= %s
            GROUP BY exchange, market_type, symbol,
                     date_bin(%s::interval, ts, TIMESTAMPTZ '1970-01-01')
            ORDER BY ts ASC, exchange ASC
            """,
            (
                f"{bucket_minutes} minutes",
                market_type,
                symbols,
                exchanges,
                since,
                f"{bucket_minutes} minutes",
            ),
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



    def funding_table(self) -> list[Row]:
        """Latest venue-published funding with settled funding as the fallback.

        Current rates ride on the five-minute liquidity snapshot and are the
        trader-facing value. Settled funding remains available in explicit
        columns and continues to anchor cadence/history consumers.

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
                       exchange, symbol, ts, open_interest, volume_24h, mark_price,
                       index_price, current_funding_rate, funding_interval_hours,
                       next_funding_at, funding_premium
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
                COALESCE(f.exchange, l.exchange)                 AS exchange,
                COALESCE(f.symbol, l.symbol)                     AS symbol,
                f.ts                                            AS funding_ts,
                COALESCE(l.current_funding_rate, f.rate)         AS rate,
                COALESCE(l.funding_interval_hours,
                         f.interval_hours)                       AS interval_hours,
                COALESCE(
                    l.current_funding_rate
                        * (8760.0 / l.funding_interval_hours) * 100,
                    f.rate * (8760.0 / f.interval_hours) * 100
                )                                               AS apr_pct,
                CASE WHEN l.current_funding_rate IS NOT NULL
                          AND l.funding_interval_hours IS NOT NULL
                     THEN 'current' ELSE 'settled' END           AS rate_kind,
                CASE WHEN l.current_funding_rate IS NOT NULL
                          AND l.funding_interval_hours IS NOT NULL
                     THEN l.ts ELSE f.ts END                     AS rate_ts,
                f.rate                                          AS settled_rate,
                f.interval_hours                                AS settled_interval_hours,
                f.rate * (8760.0 / f.interval_hours) * 100      AS settled_apr_pct,
                l.open_interest,
                l.open_interest * l.mark_price                  AS oi_notional,
                l.volume_24h, l.mark_price, l.index_price,
                l.next_funding_at, l.funding_premium,
                l.current_funding_rate,
                l.funding_interval_hours                        AS current_interval_hours,
                l.ts                                            AS liq_ts,
                CASE WHEN a.oi_24h_ago IS NOT NULL AND a.oi_24h_ago != 0
                     THEN (l.open_interest - a.oi_24h_ago) / a.oi_24h_ago * 100
                     ELSE NULL END                               AS oi_delta_pct
            FROM latest_funding f
            FULL OUTER JOIN latest_liq l
              ON l.exchange = f.exchange AND l.symbol = f.symbol
            LEFT JOIN liq_24h_ago a
              ON a.exchange = COALESCE(f.exchange, l.exchange)
             AND a.symbol = COALESCE(f.symbol, l.symbol)
            WHERE (l.current_funding_rate IS NOT NULL
                   AND l.funding_interval_hours IS NOT NULL)
               OR f.rate IS NOT NULL
            ORDER BY symbol, exchange
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

    def funding_series_multi(
        self, symbols: list[str], hours: int = 48, limit: int = 5000
    ) -> list[Row]:
        """Settled funding history across every venue that lists any of the
        given native symbols — the historic multi-venue overlay on the
        funding page. Unscoped by exchange: the caller (api layer) already
        resolved which native symbols belong to one asset via SymbolRegistry,
        so any (exchange, symbol) match here IS a real series for that asset."""
        if not symbols:
            return []
        return self._fetch(
            """
            SELECT exchange, symbol, ts, rate, interval_hours,
                   (rate * (8760.0 / interval_hours) * 100)::numeric AS apr_pct
            FROM funding_rates
            WHERE symbol = ANY(%s)
              AND ts >= now() - make_interval(hours => %s)
            ORDER BY exchange, symbol, ts
            LIMIT %s
            """,
            (symbols, hours, limit),
        )

    def liquidity_series_multi(
        self, symbols: list[str], hours: int = 48,
        bucket_minutes: int | None = None, limit: int = 20_000,
    ) -> list[Row]:
        """OI-notional history across every venue that lists any of the given
        native symbols — the volume-style bar pane under the funding history
        chart. Liquidity samples every 5min, so long windows at fine bucket
        sizes get dense fast; bucket_minutes is caller-controlled (the funding
        page exposes it directly) so a 30d view can ask for hourly/daily bars
        instead of drowning in 5min noise. Falls back to an hours-based
        default when the caller doesn't specify one."""
        if not symbols:
            return []
        if bucket_minutes is None:
            bucket_minutes = 5 if hours <= 48 else 30 if hours <= 168 else 120
        bucket = f"{bucket_minutes} minutes"
        return self._fetch(
            """
            SELECT exchange, symbol, time_bucket(%s::interval, ts) AS ts,
                   avg(open_interest * mark_price) AS oi_notional
            FROM liquidity
            WHERE symbol = ANY(%s)
              AND ts >= now() - make_interval(hours => %s)
            GROUP BY exchange, symbol, 3
            ORDER BY exchange, symbol, 3
            LIMIT %s
            """,
            (bucket, symbols, hours, limit),
        )

    def volume_oi_ratio(
        self, hours: int = 48, bucket_minutes: int | None = None,
        limit: int = 20_000,
    ) -> list[Row]:
        """Reported 24h perp volume divided by OI notional, per exchange.

        Each market contributes its latest snapshot inside the bucket. Venue
        totals are calculated before division so small markets cannot carry
        the same weight as large ones.
        """
        if bucket_minutes is None:
            bucket_minutes = 15 if hours <= 48 else 60 if hours <= 168 else 240
        bucket = f"{bucket_minutes} minutes"
        return self._fetch(
            """
            WITH market_buckets AS (
                SELECT exchange, symbol, time_bucket(%s::interval, ts) AS ts,
                       last(volume_24h, ts) AS volume_24h,
                       last(open_interest * mark_price, ts) AS oi_notional
                FROM liquidity
                WHERE ts >= now() - make_interval(hours => %s)
                  AND volume_24h IS NOT NULL
                  AND volume_24h >= 0
                  AND open_interest > 0
                  AND mark_price > 0
                GROUP BY exchange, symbol, 3
            ),
            venue_buckets AS (
                SELECT exchange, ts,
                       sum(volume_24h) AS volume_24h,
                       sum(oi_notional) AS oi_notional
                FROM market_buckets
                GROUP BY exchange, ts
            )
            SELECT exchange, ts, volume_24h, oi_notional,
                   volume_24h / NULLIF(oi_notional, 0) AS ratio
            FROM venue_buckets
            WHERE oi_notional > 0
            ORDER BY exchange, ts
            LIMIT %s
            """,
            (bucket, hours, limit),
        )


    def trade_flow_multi(
        self, symbols: list[str], hours: int = 48,
        bucket_minutes: int | None = None, limit: int = 20_000,
    ) -> list[Row]:
        """Per-venue order flow for one canonical asset, from the trades_1m
        continuous aggregate (migration 007) — never from the raw tape, which
        is millions of rows a day.

        Re-buckets the 1m aggregate up to `bucket_minutes`; the caller resolves
        which venue-native symbols belong to one asset via SymbolRegistry, same
        contract as funding_series_multi and liquidity_series_multi.

        imbalance_pct is (buys - sells) / total volume: the directional
        pressure measure, positive when takers are lifting offers. It is the
        headline number for settlement behaviour, so it is computed here rather
        than left to every caller to get right.
        """
        if not symbols:
            return []
        if bucket_minutes is None:
            bucket_minutes = 1 if hours <= 6 else 5 if hours <= 48 else 60
        bucket = f"{bucket_minutes} minutes"
        return self._fetch(
            """
            SELECT exchange, symbol,
                   time_bucket(%s::interval, bucket) AS ts,
                   sum(trades)       AS trades,
                   sum(volume)       AS volume,
                   sum(buy_volume)   AS buy_volume,
                   sum(sell_volume)  AS sell_volume,
                   sum(notional)     AS notional,
                   sum(large_trades) AS large_trades,
                   sum(large_buy_volume)  AS large_buy_volume,
                   sum(large_sell_volume) AS large_sell_volume,
                   max(max_trade_notional) AS max_trade_notional,
                   CASE WHEN sum(volume) > 0
                        THEN (sum(buy_volume) - sum(sell_volume))
                             / sum(volume) * 100
                        ELSE NULL END AS imbalance_pct,
                   CASE WHEN sum(large_buy_volume) + sum(large_sell_volume) > 0
                        THEN (sum(large_buy_volume) - sum(large_sell_volume))
                             / (sum(large_buy_volume) + sum(large_sell_volume)) * 100
                        ELSE NULL END AS large_imbalance_pct
            FROM trades_1m
            WHERE symbol = ANY(%s)
              AND bucket >= now() - make_interval(hours => %s)
            GROUP BY exchange, symbol, 3
            ORDER BY exchange, symbol, 3
            LIMIT %s
            """,
            (bucket, symbols, hours, limit),
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