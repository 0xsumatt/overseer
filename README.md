# overseer

Market-data collection + dashboard for crypto venues (Binance, Bybit,
Hyperliquid, Lighter, Extended). Two independent processes sharing one
TimescaleDB:

* **scheduler** — async ingest: OHLCV bars, settled funding, OI/volume and
  top-of-book snapshots, plus tracked-address fills, per `symbols.toml`.
  Writes only.
* **web** — Flask public analytics dashboard (price, bid–ask spread, and
  reported-volume/open-interest ratio charts; spot-perp basis; funding; flow;
  wallets) plus an authenticated internal health page. Reads only. Optional;
  ingest runs fine without it.

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
| `DISCORD_WEBHOOK_URL` | job health, current-funding dislocations with executable BBOs, and daily digest (optional) |
| `FLASK_SECRET_KEY` | web session signing; set for any non-dev deploy |
| `OVERSEER_SYMBOLS_FILE` | path to symbols.toml (default: ./symbols.toml, cwd-relative) |
| `OVERSEER_SPREAD_ALERT_APR` | APR spread threshold that starts confirmation (default: 25) |
| `OVERSEER_SPREAD_CONFIRM_MINUTES` | minutes a spread must remain wide across fresh snapshots before alerting (default: 10) |

What gets scraped lives in `symbols.toml` (venues × assets); it is validated
at startup and the scheduler refuses to launch on a bad config.
`[asset_metadata.*]` supplies funding-dashboard family, class, group, and display
labels. `[hyperliquid_deployers.*]` labels HIP-3 namespaces such as TradeXYZ
without treating them as separate physical exchanges.
Set `orderbook = true` on a venue to retain one best-bid/ask snapshot per
configured market every 60 seconds. Override that cadence with
`orderbook_poll_seconds`.

## Run

```sh
# ingest (the soak workload)
uv run overseer-scheduler

# web dashboard
uv run flask --app web:create_app run --debug                         # dev
uv run gunicorn --config deploy/gunicorn.conf.py "web:create_app()"  # prod smoke

# only the protected /health page uses accounts; analytics routes are public:
uv run flask --app web:create_app create-user you@example.com --internal
```

## Deploying the web app with Gunicorn and Nginx

The checked-in deployment assumes Debian/Ubuntu, a checkout at
`/opt/overseer`, and an `overseer` system user. Nginx is the public process;
Gunicorn listens only on `127.0.0.1:8000`.

1. Install the locked Python environment. This puts Gunicorn at the exact path
   used by systemd:

   ```sh
   cd /opt/overseer
   uv sync --frozen
   ```

2. Populate `/opt/overseer/.env`. `DATABASE_URL` and a stable,
   randomly-generated `FLASK_SECRET_KEY` are required. The service file sets
   `OVERSEER_PROXY_HOPS=1` and `OVERSEER_SECURE_COOKIES=true`; local Flask
   development leaves both disabled.

3. Install and start the Gunicorn service:

   ```sh
   sudo install -m 0644 deploy/systemd/overseer-web.service \
     /etc/systemd/system/overseer-web.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now overseer-web
   curl -I http://127.0.0.1:8000/
   ```

   Gunicorn runs two synchronous workers. Each worker creates its own database
   pool and 30-second response cache; the scheduler remains a separate process.

4. Install Nginx and activate the site:

   ```sh
   sudo apt-get update
   sudo apt-get install nginx
   sudo install -m 0644 deploy/nginx/overseer.conf \
     /etc/nginx/sites-available/overseer
   sudo ln -sfn /etc/nginx/sites-available/overseer \
     /etc/nginx/sites-enabled/overseer
   # Remove /etc/nginx/sites-enabled/default if it is the stock distro symlink.
   sudo nginx -t
   sudo systemctl reload nginx
   ```

   Nginx serves `/static/` directly and proxies every other request to
   Gunicorn. Port 8000 stays private; only ports 80 and 443 belong in the
   public firewall.

5. Before exposing login, replace `server_name _;` in the installed Nginx file
   with the real hostname and install TLS:

   ```sh
   sudo apt-get install certbot python3-certbot-nginx
   sudo certbot --nginx -d dashboard.example.com
   sudo certbot renew --dry-run
   ```

   Use the actual hostname in place of `dashboard.example.com`. Certbot adds
   the certificate paths and HTTP-to-HTTPS redirect. Secure session cookies
   deliberately prevent login over plain HTTP.

6. Operate and inspect the service:

   ```sh
   sudo systemctl status overseer-web nginx
   sudo systemctl reload overseer-web     # graceful Gunicorn worker replacement
   journalctl -u overseer-web -f
   sudo nginx -t                          # always run before reloading Nginx
   ```

Request flow: browser → Nginx/TLS → Gunicorn → Flask → TimescaleDB. Nginx
overwrites the forwarded client and scheme headers; Flask trusts exactly one
proxy hop.

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
* History jobs resume from the newest stored row and all writes use natural-key
  conflict handling. Restarts cannot duplicate an existing market-data event.
* Logs go to stdout → journald: `journalctl -u overseer-scheduler -f`.

## Monitoring a soak

* Discord: job alerts fire on ok→fail and fail→ok transitions. A current-funding
  spread must remain above the threshold across at least three fresh snapshots
  for the configured confirmation window. Both legs must have known next
  settlements within one minute of each other; only then are live BBOs fetched
  and an alert sent. A daily digest posts at 08:00 UTC.
* `job_runs` table: per-job heartbeat (`last_status`, `last_error`,
  `last_success_at`) — `SELECT * FROM job_runs WHERE last_status='fail'`.
* Freshness (catches jobs that "succeed" while a venue quietly returns
  nothing): the `/health` page on the web app, or directly in SQL —
  age of the newest bar per series should stay near the bar interval.
