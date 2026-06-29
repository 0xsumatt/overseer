

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------------
-- OHLCV bars — backfillable, the durable source of truth for charts.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ohlcv (
    exchange     TEXT        NOT NULL,
    market_type  TEXT        NOT NULL,
    symbol       TEXT        NOT NULL,
    interval     TEXT        NOT NULL,
    ts           TIMESTAMPTZ NOT NULL,          -- bar open time, UTC
    open         NUMERIC     NOT NULL,
    high         NUMERIC     NOT NULL,
    low          NUMERIC     NOT NULL,
    close        NUMERIC     NOT NULL,
    volume       NUMERIC     NOT NULL,
    -- Natural key. `ts` is part of it because a hypertable's unique index must
    -- contain the partitioning column — and it's dedup-safe, since a bar is
    -- uniquely identified by venue+market+symbol+interval+open-time anyway.
    -- market_type is in the key so the SAME symbol on spot and perp never
    -- collides (the reason market_type is a column, not part of the symbol).
    PRIMARY KEY (exchange, market_type, symbol, interval, ts)
);
SELECT create_hypertable('ohlcv', 'ts', if_not_exists => TRUE);

-- ---------------------------------------------------------------------------
-- Trades — populated later by the websocket layer; defined now so the schema
-- is coherent. wallet_address is nullable (only on-chain venues set it); the
-- WS phase may split it into maker/taker addresses.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trades (
    exchange       TEXT        NOT NULL,
    market_type    TEXT        NOT NULL,
    symbol         TEXT        NOT NULL,
    trade_id       TEXT        NOT NULL,
    ts             TIMESTAMPTZ NOT NULL,
    price          NUMERIC     NOT NULL,
    amount         NUMERIC     NOT NULL,
    side           TEXT        NOT NULL,
    wallet_address TEXT        NULL,
    -- (…, trade_id, ts): ts again for the hypertable requirement; still
    -- dedup-correct because a given trade_id has exactly one ts.
    PRIMARY KEY (exchange, market_type, symbol, trade_id, ts)
);
SELECT create_hypertable('trades', 'ts', if_not_exists => TRUE);

-- Partial index for wallet-attribution queries: the column is mostly NULL, so
-- index only the rows that have a wallet — stays tiny, exactly the slice hit.
CREATE INDEX IF NOT EXISTS idx_trades_wallet
    ON trades (wallet_address) WHERE wallet_address IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Compression — tick/bar data compresses extremely well; compress chunks once
-- they age past the window. segmentby groups same-series rows for best ratios.
-- ---------------------------------------------------------------------------
ALTER TABLE ohlcv  SET (timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange, market_type, symbol, interval');
ALTER TABLE trades SET (timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange, market_type, symbol');
SELECT add_compression_policy('ohlcv',  INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('trades', INTERVAL '7 days', if_not_exists => TRUE);

-- Optional retention: drop raw trades older than 90 days (bars are kept).
-- SELECT add_retention_policy('trades', INTERVAL '90 days', if_not_exists => TRUE);

-- ---------------------------------------------------------------------------
-- Continuous aggregate — roll 1m bars up to 1h, kept fresh incrementally so the
-- dashboard never recomputes candles on read. GROUP BY market_type so spot and
-- perp on a same-symbol venue never blend into one bogus bar.
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS ohlcv_1h
WITH (timescaledb.continuous) AS
SELECT
    exchange,
    market_type,
    symbol,
    time_bucket(INTERVAL '1 hour', ts) AS bucket,
    first(open, ts) AS open,
    max(high)       AS high,
    min(low)        AS low,
    last(close, ts) AS close,
    sum(volume)     AS volume
FROM ohlcv
WHERE interval = '1m'
GROUP BY exchange, market_type, symbol, bucket
WITH NO DATA;

SELECT add_continuous_aggregate_policy('ohlcv_1h',
    start_offset      => INTERVAL '3 hours',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists     => TRUE);