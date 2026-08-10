-- ---------------------------------------------------------------------------
-- One best-bid/ask snapshot per scheduled venue market poll.
--
-- This is deliberately top-of-book, not full depth. At one row per market per
-- minute, storage grows with configured coverage rather than message traffic,
-- while retaining the executable spread and displayed size needed by charts.
-- Raw venue timestamps are kept: consumers compare each venue with itself, so
-- no false cross-venue synchronization is implied.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    exchange       TEXT        NOT NULL,
    market_type    TEXT        NOT NULL,
    symbol         TEXT        NOT NULL,
    quote_currency TEXT        NOT NULL,
    ts             TIMESTAMPTZ NOT NULL,
    bid_price      NUMERIC     NOT NULL CHECK (bid_price > 0),
    bid_size       NUMERIC     NOT NULL CHECK (bid_size > 0),
    ask_price      NUMERIC     NOT NULL CHECK (ask_price > 0),
    ask_size       NUMERIC     NOT NULL CHECK (ask_size > 0),
    CHECK (bid_price <= ask_price),
    PRIMARY KEY (exchange, market_type, symbol, ts)
);

SELECT create_hypertable('orderbook_snapshots', 'ts', if_not_exists => TRUE);

ALTER TABLE orderbook_snapshots SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange, market_type, symbol'
);
SELECT add_compression_policy(
    'orderbook_snapshots', INTERVAL '7 days', if_not_exists => TRUE
);
