
CREATE TABLE IF NOT EXISTS job_runs (
    job_id          TEXT        PRIMARY KEY,
    last_run_at     TIMESTAMPTZ NOT NULL,
    last_success_at TIMESTAMPTZ,                 -- preserved across failures
    last_status     TEXT        NOT NULL,        -- 'ok' | 'fail'
    fetched         INTEGER     NOT NULL DEFAULT 0,
    new_rows        INTEGER     NOT NULL DEFAULT 0,
    last_error      TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);