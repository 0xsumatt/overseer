-- ---------------------------------------------------------------------------
-- Leveraged-token stats — the non-price series behind a leveraged token
-- (Hylo xSOL/xBTC today; the table is venue-generic so a second protocol can
-- land here without another migration).
--
-- NAV itself is NOT here: it arrives as OHLC bars and lives in `ohlcv` under
-- market_type = 'leveraged_token', which buys the charts, freshness view and
-- compression policy for free. This table is for the two series that have no
-- home in the existing schema:
--
--   total_value — the collateral pool's TVL (Hylo's exo-total-value, the
--                 series the docs treat as the pool's open interest).
--   leverage    — effective leverage (Hylo's exo-leverage-ratio), i.e.
--                 ASSET TVL / xASSET market cap.
--
-- Collateral ratio is deliberately NOT stored: it is a pure function of
-- leverage (CR = L / (L - 1)) and the codebase's convention is to store what
-- the venue reports and derive the rest at read time — same reason `liquidity`
-- keeps open_interest and mark_price apart instead of storing notional.
-- That identity is what makes leverage alone sufficient to recover Hylo's
-- fee zone: neutral band 135-165% CR == leverage 2.54x-3.86x.
--
-- Both metrics are NULLABLE. They come from two separate endpoints and we have
-- not yet been able to confirm (the API is behind a challenge — see
-- exchanges/hylo.py) that their bar timestamps line up, so a row is written
-- when EITHER series has a value at that timestamp rather than dropping the
-- pair. Revisit and tighten to NOT NULL once alignment is observed live.
--
-- `interval` is in the key because these endpoints are timeframe-parameterised
-- like a chart feed: a 1d backfill and a 15m live poll for the same token are
-- different series, exactly as in `ohlcv`. Read queries must always filter on
-- it or they will blend granularities.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leveraged_token_stats (
    exchange    TEXT        NOT NULL,
    symbol      TEXT        NOT NULL,          -- token symbol, e.g. 'xBTC'
    interval    TEXT        NOT NULL,          -- bar size, e.g. '15m', '1d'
    ts          TIMESTAMPTZ NOT NULL,          -- bar open time, UTC
    total_value NUMERIC,                       -- pool TVL, quote notional
    leverage    NUMERIC,                       -- effective leverage, e.g. 3.0
    PRIMARY KEY (exchange, symbol, interval, ts)
);
SELECT create_hypertable('leveraged_token_stats', 'ts', if_not_exists => TRUE);

-- Same compression posture as the data tables in 001/003: segment by series so
-- same-series rows compress together.
ALTER TABLE leveraged_token_stats SET (timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange, symbol, interval');
SELECT add_compression_policy('leveraged_token_stats', INTERVAL '7 days',
                              if_not_exists => TRUE);
