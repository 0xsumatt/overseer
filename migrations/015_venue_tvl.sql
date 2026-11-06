-- ---------------------------------------------------------------------------
-- Venue TVL in USD from DefiLlama (data_collection/defillama.py), one row per
-- exchange per 15-minute poll. The scheduler alerts when the latest reading
-- falls sharply below the trailing peak, which can mean an exploit or a run.
-- About 800 rows a day across every venue, so no compression policy.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS venue_tvl (
    exchange TEXT        NOT NULL,
    ts       TIMESTAMPTZ NOT NULL,
    tvl_usd  NUMERIC     NOT NULL CHECK (tvl_usd > 0),
    PRIMARY KEY (exchange, ts)
);
SELECT create_hypertable('venue_tvl', 'ts', if_not_exists => TRUE);
