-- ---------------------------------------------------------------------------
-- Order-book depth near the mid, for historic heatmaps and backtests.
--
-- One row per stored book: resting base quantity in fixed buckets either side
-- of the book's own mid (index 0 is nearest the mid), how far the book itself
-- reached on each side, and the mid that turns buckets back into prices.
-- Beyond a side's reach the book showed nothing, which is not the same as
-- nothing resting there. The scheduler writes these from the same five-minute
-- capture as execution_samples (source 'rest'); source and bucket width are
-- per row so a finer or fuller feed can share the table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS depth_snapshots (
    ts               TIMESTAMPTZ NOT NULL,
    asset            TEXT        NOT NULL,
    venue            TEXT        NOT NULL,
    source           TEXT        NOT NULL,
    bucket_bps       REAL        NOT NULL CHECK (bucket_bps > 0),
    mid              NUMERIC     NOT NULL CHECK (mid > 0),
    quote_currency   TEXT        NOT NULL,
    usd_rate         NUMERIC     CHECK (usd_rate > 0),
    book_age_seconds NUMERIC,
    bid_reach_bps    REAL        NOT NULL,
    ask_reach_bps    REAL        NOT NULL,
    bid_levels       INT         NOT NULL,
    ask_levels       INT         NOT NULL,
    bids             REAL[]      NOT NULL,
    asks             REAL[]      NOT NULL,
    PRIMARY KEY (asset, venue, source, ts)
);

SELECT create_hypertable('depth_snapshots', 'ts', if_not_exists => TRUE);

ALTER TABLE depth_snapshots SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'asset, venue, source'
);
SELECT add_compression_policy(
    'depth_snapshots', INTERVAL '7 days', if_not_exists => TRUE
);
