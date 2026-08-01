-- ---------------------------------------------------------------------------
-- Per-minute order flow, written directly by the stream layer.
--
-- NOTE: an earlier draft of this file (same day, likely never applied) created
-- trades_1m as a TimescaleDB continuous aggregate over the raw trades table.
-- That only works if the whole tape is stored, which it deliberately is not —
-- see below. The DROP makes this file safe to run either way; it removes an
-- empty view, never real data. If you already applied the earlier version and
-- somehow accumulated rows in it, check before running.
-- ---------------------------------------------------------------------------
DROP MATERIALIZED VIEW IF EXISTS trades_1m CASCADE;

-- ---------------------------------------------------------------------------
-- Why this is a written table and not an aggregate over stored trades
-- -------------------------------------------------------------------
-- Storing the full tape was measured on 2026-07-27 at ~1.35 GB/day (including
-- the primary-key index) for just five assets across two venues — ~9.5 GB a
-- week — and it scales with how much the market TRADES, not with how many
-- markets we follow. On Bybit, widening from 5 to 30 symbols multiplied the
-- row rate by 5.6x, and rank by turnover turned out to be a poor predictor:
-- one high-churn listing produced more rows than ten majors combined. That is
-- an unbounded, unpredictable cost for data that is not a differentiator —
-- anyone needing raw ticks collects them or buys them from Tardis.
--
-- So the stream aggregates in memory and writes only completed minutes. This
-- table costs exactly 1440 rows per market per day whatever the market does:
-- ~1.2 MB/day at the current ten series, ~24 MB/day even at 210 series. The
-- cost becomes a function of coverage, which is a number we choose.
--
-- Raw trades are still written, but only inside funding-settlement windows
-- (~1% of the tape) where tick granularity is the actual research object.
--
-- The trade-off accepted here: metrics not computed at ingest cannot be
-- back-derived outside those windows. Adding a column later means it is NULL
-- for all history. That is why buy/sell volume is captured now — signed order
-- flow is the whole point, and it cannot be recovered from OHLC.
--
-- open/close are first/last by TRADE time rather than arrival, since venues
-- deliver slightly out of order and replay on reconnect.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trades_1m (
    exchange    TEXT        NOT NULL,
    market_type TEXT        NOT NULL,
    symbol      TEXT        NOT NULL,
    bucket      TIMESTAMPTZ NOT NULL,      -- minute start, UTC
    trades      INT         NOT NULL,
    volume      NUMERIC     NOT NULL,      -- base units
    buy_volume  NUMERIC     NOT NULL,      -- taker-buy base units
    sell_volume NUMERIC     NOT NULL,
    notional    NUMERIC     NOT NULL,      -- sum(price * amount), quote
    -- Size participation. Volume and trade count together cannot tell one
    -- institutional clip from a thousand retail ones, and around a settlement
    -- that is the question. "Large" is >= 10,000 quote notional, applied at
    -- ingest (storage/buffer.py) and NOT changeable retroactively, since the
    -- trades it classifies are not kept. max_trade_notional is threshold-free
    -- and hedges a badly chosen threshold.
    large_trades       INT     NOT NULL DEFAULT 0,
    large_buy_volume   NUMERIC NOT NULL DEFAULT 0,   -- base units
    large_sell_volume  NUMERIC NOT NULL DEFAULT 0,
    max_trade_notional NUMERIC NOT NULL DEFAULT 0,   -- biggest single trade
    open        NUMERIC     NOT NULL,
    high        NUMERIC     NOT NULL,
    low         NUMERIC     NOT NULL,
    close       NUMERIC     NOT NULL,
    PRIMARY KEY (exchange, market_type, symbol, bucket)
);
SELECT create_hypertable('trades_1m', 'bucket', if_not_exists => TRUE);

ALTER TABLE trades_1m SET (timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange, market_type, symbol');
SELECT add_compression_policy('trades_1m', INTERVAL '30 days', if_not_exists => TRUE);

-- ---------------------------------------------------------------------------
-- Retention on the RAW tape — still commented out, but now far less pressing:
-- with writes gated to settlement windows the tape grows at roughly 14 MB/day
-- rather than 1.35 GB/day, so a year is a few GB rather than half a terabyte.
-- Uncomment if you want a hard bound anyway. trades_1m is NOT affected by
-- this policy and keeps full flow history regardless.
-- ---------------------------------------------------------------------------
-- SELECT add_retention_policy('trades', INTERVAL '365 days', if_not_exists => TRUE);
