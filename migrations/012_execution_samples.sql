-- ---------------------------------------------------------------------------
-- Cheapest-venue history for the execution estimator.
--
-- Every five minutes the scheduler captures public depth for a fixed coin list
-- and stores each venue's PRE-FEE result per side and trade size: the average
-- USD fill when the quote was complete and fresh, or the reason it was not
-- comparable. Fees are applied when statistics are read, so they can follow
-- base or user-selected tiers and a fee-schedule change never rewrites history.
-- Rows sharing a ts belong to one capture.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS execution_samples (
    ts                TIMESTAMPTZ NOT NULL,
    asset             TEXT        NOT NULL,
    side              TEXT        NOT NULL CHECK (side IN ('buy', 'sell')),
    notional_usd      NUMERIC     NOT NULL CHECK (notional_usd > 0),
    venue             TEXT        NOT NULL,
    status            TEXT        NOT NULL,
    average_price_usd NUMERIC     CHECK (average_price_usd > 0),
    book_age_seconds  NUMERIC,
    CHECK ((status = 'ok') = (average_price_usd IS NOT NULL)),
    PRIMARY KEY (asset, side, notional_usd, venue, ts)
);

SELECT create_hypertable('execution_samples', 'ts', if_not_exists => TRUE);

ALTER TABLE execution_samples SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'asset, side, notional_usd, venue'
);
SELECT add_compression_policy(
    'execution_samples', INTERVAL '7 days', if_not_exists => TRUE
);
