-- ---------------------------------------------------------------------------
-- Funding rates — settled funding payments per (venue, perp). Perp-domain only.
-- interval_hours travels with every row because venues settle on different
-- cadences (HL hourly, Binance 8h/4h); consumers annualize with
-- rate * (8760 / interval_hours). Natural key (exchange, symbol, ts) — ts is
-- the settlement time, so it is dedup-correct and satisfies the hypertable
-- unique-index requirement in one go.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS funding_rates (
    exchange       TEXT        NOT NULL,
    symbol         TEXT        NOT NULL,
    ts             TIMESTAMPTZ NOT NULL,          -- settlement time, UTC
    rate           NUMERIC     NOT NULL,          -- per-interval rate
    interval_hours INT         NOT NULL,
    PRIMARY KEY (exchange, symbol, ts)
);
SELECT create_hypertable('funding_rates', 'ts', if_not_exists => TRUE);

-- ---------------------------------------------------------------------------
-- Liquidity snapshots — point-in-time OI / 24h volume / mark per (venue, perp).
-- ts is our poll clock, so (exchange, symbol, ts) is naturally unique.
-- open_interest is BASE units; notional = open_interest * mark_price.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS liquidity (
    exchange      TEXT        NOT NULL,
    symbol        TEXT        NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,          -- snapshot time (our clock)
    open_interest NUMERIC     NOT NULL,          -- base units
    volume_24h    NUMERIC     NOT NULL,          -- quote notional
    mark_price    NUMERIC     NOT NULL,
    PRIMARY KEY (exchange, symbol, ts)
);
SELECT create_hypertable('liquidity', 'ts', if_not_exists => TRUE);

-- Same compression posture as the data tables in 001: segment by series so
-- same-series rows compress together.
ALTER TABLE funding_rates SET (timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange, symbol');
ALTER TABLE liquidity SET (timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange, symbol');
SELECT add_compression_policy('funding_rates', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('liquidity',     INTERVAL '7 days', if_not_exists => TRUE);
