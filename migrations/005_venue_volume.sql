
CREATE TABLE IF NOT EXISTS venue_volume (
    exchange     TEXT        NOT NULL,
    ts           TIMESTAMPTZ NOT NULL,
    volume_total NUMERIC     NOT NULL,
    volume_spot  NUMERIC,
    volume_perp  NUMERIC,
    PRIMARY KEY (exchange, ts)
);
SELECT create_hypertable('venue_volume', 'ts', if_not_exists => TRUE);