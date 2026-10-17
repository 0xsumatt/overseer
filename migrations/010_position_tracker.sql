-- Private, read-only account snapshots and settled funding ledgers. These are
-- ordinary PostgreSQL tables: deleting a login or saved account removes its data.
CREATE TABLE IF NOT EXISTS position_accounts (
    id                       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id                  BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    venue                    TEXT NOT NULL CHECK (
        venue IN ('hyperliquid', 'lighter', 'bulk', 'risex')
    ),
    address                  TEXT NOT NULL,
    label                    TEXT NOT NULL,
    token_ciphertext         TEXT,
    snapshot                 JSONB,
    positions_checked_at     TIMESTAMPTZ,
    positions_attempted_at   TIMESTAMPTZ,
    positions_error          TEXT,
    positions_failures       INTEGER NOT NULL DEFAULT 0 CHECK (positions_failures >= 0),
    funding_checked_at       TIMESTAMPTZ,
    funding_attempted_at     TIMESTAMPTZ,
    funding_error            TEXT,
    funding_failures         INTEGER NOT NULL DEFAULT 0 CHECK (funding_failures >= 0),
    funding_since            TIMESTAMPTZ,
    funding_cursor           JSONB,
    funding_coverage_note    TEXT,
    funding_token_version    BIGINT NOT NULL DEFAULT 0,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, venue, address),
    CHECK (token_ciphertext IS NULL OR venue = 'lighter')
);

CREATE TABLE IF NOT EXISTS position_funding_events (
    account_id BIGINT NOT NULL REFERENCES position_accounts(id) ON DELETE CASCADE,
    event_id   TEXT NOT NULL,
    ts         TIMESTAMPTZ NOT NULL,
    symbol     TEXT NOT NULL,
    amount     NUMERIC NOT NULL,
    currency   TEXT NOT NULL,
    scope      TEXT NOT NULL DEFAULT 'main',
    PRIMARY KEY (account_id, event_id)
);

CREATE INDEX IF NOT EXISTS position_funding_events_account_ts
    ON position_funding_events (account_id, ts);

-- Rates are USD per one unit of the named currency. They are current market
-- estimates, never payment-time prices or assumed stablecoin pegs.
CREATE TABLE IF NOT EXISTS position_fx (
    id     BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    rates  JSONB NOT NULL,
    ts     TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL
);
