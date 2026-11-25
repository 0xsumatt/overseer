-- ---------------------------------------------------------------------------
-- Markets each configured venue lists, refreshed by the daily listings check
-- (scheduler/listings.py). It reports tracked assets a venue newly lists,
-- configured markets a venue stops listing, and, weekly, untracked assets
-- spreading across venues. A few thousand rows, rewritten once a day, so a
-- plain table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS venue_listings (
    venue      TEXT        NOT NULL,   -- symbols.toml venue id, e.g. bybit_perp
    symbol     TEXT        NOT NULL,   -- as symbols.toml writes it for the venue
    base       TEXT        NOT NULL,   -- the underlying as the venue names it
    first_seen TIMESTAMPTZ,            -- NULL: listed before the venue's first check
    last_seen  TIMESTAMPTZ,            -- NULL: configured but never seen listed
    listed     BOOLEAN     NOT NULL,   -- FALSE once the venue stops listing it
    PRIMARY KEY (venue, symbol)
);
