# overseer

Market-data collection + dashboard for crypto venues (Binance, Bybit,
Hyperliquid, Lighter, Extended). Two independent processes sharing one
TimescaleDB:

* **scheduler** — async ingest: OHLCV bars, settled funding, OI/volume
  snapshots, and tracked-address fills, per `symbols.toml`. Writes only.
* **web** — Flask dashboard (charts, spot-perp basis, funding table,
  internal health page). Reads only. Optional; ingest runs fine without it.

## Setup

```sh
uv sync

# fresh database (TimescaleDB required), then apply migrations IN ORDER:
for f in migrations/*.sql; do psql "$DATABASE_URL" -f "$f"; done
```

Configuration is env-driven (see `.env.example` — nothing auto-loads a .env
file; export the vars or use `uv run --env-file .env`):

| var | purpose |
|---|---|
| `DATABASE_URL` | Timescale DSN, both processes |
| `DISCORD_WEBHOOK_URL` | failure/recovery alerts + daily digest (optional but strongly recommended for soak runs) |
| `FLASK_SECRET_KEY` | web session signing; set for any non-dev deploy |
| `OVERSEER_SYMBOLS_FILE` | path to symbols.toml (default: ./symbols.toml, cwd-relative) |

What gets scraped lives in `symbols.toml` (venues × assets); it is validated
at startup and the scheduler refuses to launch on a bad config.

## Run

```sh
# ingest (the soak workload)
uv run overseer-scheduler

# web dashboard (optional)
uv run flask --app web:create_app run --debug                  # dev
uv run gunicorn "web:create_app()" -w 2 -b 127.0.0.1:8000      # prod

# web accounts are CLI-only (no self-registration):
uv run flask --app web:create_app create-user you@example.com --internal
```

## Deploying the scheduler under systemd

`/etc/systemd/system/overseer-scheduler.service`:

```ini
[Unit]
Description=overseer market-data scheduler
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
User=overseer
WorkingDirectory=/opt/overseer
EnvironmentFile=/opt/overseer/.env
ExecStart=/usr/local/bin/uv run overseer-scheduler
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Notes:

* `WorkingDirectory` matters: `symbols.toml` is resolved relative to cwd.
* The scheduler shuts down cleanly on SIGTERM (systemd's default stop signal).
* Restarts are safe: writes are idempotent upserts and every job resumes from
  the newest row in the database, so a crash-loop can duplicate nothing.
* Logs go to stdout → journald: `journalctl -u overseer-scheduler -f`.

## Monitoring a soak

* Discord: alerts fire on a job's ok→fail and fail→ok transitions only, plus
  a daily digest at 08:00 UTC.
* `job_runs` table: per-job heartbeat (`last_status`, `last_error`,
  `last_success_at`) — `SELECT * FROM job_runs WHERE last_status='fail'`.
* Freshness (catches jobs that "succeed" while a venue quietly returns
  nothing): the `/health` page on the web app, or directly in SQL —
  age of the newest bar per series should stay near the bar interval.
