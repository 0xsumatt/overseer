-- ---------------------------------------------------------------------------
-- Web accounts. No self-registration: rows are created only via the
-- `flask --app web:create_app create-user` CLI (see src/web/__init__.py).
-- can_view_internal gates the /health page (role_required decorator).
-- Plain table, not a hypertable — a handful of rows, no time dimension.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email             TEXT        NOT NULL UNIQUE,
    password_hash     TEXT        NOT NULL,       -- werkzeug scrypt hash
    can_view_internal BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
