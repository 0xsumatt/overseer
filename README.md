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
without treating them as separate physical exchanges. Display names use the
`hip3:` prefix and the configured deployer name, e.g. `hip3:tradexyz` and
`hip3:entropy`; native markets remain `hyperliquid`. These labels do not change
venue identifiers, data lookups, or saved exchange selections.
Funding columns size to their labels so deployer names remain fully visible;
the table scrolls horizontally on narrow screens.
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

## UI development

Preview without a database using the built-in generated data:

```sh
npm ci
npm run css
OVERSEER_MOCK=1 uv run flask --app web:create_app run --debug
```

Mock login for the internal health page: `mock@overseer.local` / `mock`.
Mock values and freshness/job states are generated, not observations of live
markets. Leave `OVERSEER_MOCK` unset for a database-backed deployment.

Shared controls and cards live in `src/web/static/css/assets.css`; rebuild
`app.css` with `npm run css` after template, JavaScript-class, or style changes.
Use native selects with `select-shell` / `select-polished`, 44px
`toolbar-button` controls, and the dense `compact-button` variant. Segmented
selectors use 32px desktop buttons with 14px text; below 1280px they retain
44px touch targets. Dropdown heights are unchanged.
`chart-card`, `summary-card`, `table-card`, and `chart-caption` share presentation;
chart heights and table scrolling remain specific to their data.
Venue identity colours come from `venues.js`; signed values use gain/loss tones,
with zero neutral. Unselected venue controls remain readable and operable.

The shared Market feed strip leads with the age of the newest ingested bar,
its UTC timestamp, and an explicit Fresh / Delayed / Stale state. It measures
pipeline freshness across all markets, not every displayed chart or funding rate.
Age advances between checks; existing thresholds remain 2 minutes for fresh
and 5 minutes before stale. Screen-refresh activity is labelled separately and
never replaces the data age. Failed checks and offline states retain the
last-confirmed bar and its advancing age; absent data or an unsuccessful first
check shows no invented age or timestamp. The timer is not a live announcement,
while stale/error warnings remain accessible through the warning banner.

Navigation keeps Charts, Basis, and Funding as primary pages. Tools opens a
disclosure containing Trade flow and Tracked wallets; both retain their direct
URLs and independent filters. Tools is highlighted on either page. The disclosure
supports normal Tab navigation, arrow-key entry, Escape, and outside/focus-leave
dismissal; on mobile, Escape closes Tools before closing the main menu.

Page scope is explicit: Flow thresholds filter only its detail table, which
shows at most the latest 400 matching venue-minute rows; Wallets share meters
cover 24h across all assets and summaries cover all recorded fills. Funding
history labels raw settlement values separately from smoothed chart lines.

Charts offers Price, Spread, and Volatility metrics in both Overview and the
multi-chart view. Volatility plots rolling realised volatility by venue; its
saved 1h/6h/24h calculation window defaults to 24h. The History selector
(24h/48h/7d) controls the history displayed, independently of the rolling window.
`/api/realised-volatility` reads completed 1-minute candles plus the warm-up
history and computes `100 * sqrt(sum(log(close / previous_close)^2))`, without
annualisation. Plot points are sampled every 1/5/15 minutes for 24h/48h/7d views,
but every calculation still uses the underlying 1-minute returns.

Volatility keeps a short non-annualised caption visible; the keyboard-accessible
"How it's calculated" disclosure contains the formula and missing-data explanation.
On narrow screens, all metric labels fit their selector while retaining
44px touch targets.

The coloured exchange buttons below the filters toggle each venue immediately
across all Price, Spread, and Volatility charts. Unselected buttons remain visible
and clickable, including when no venues are selected. Selected buttons have a
gold outline and fill; venue colours remain unchanged. The Exchanges dropdown
provides bulk selection and uses the same global state. There are no per-chart
exchange or custom/reset controls.
The global choice persists across reloads and applies to newly added assets.
Missing venues stay selected globally, while each chart reports how many selected
venues have data. Saved per-chart overrides no longer affect rendering.

Hover the plot to see per-venue values at the crosshair; on touch screens,
long-press the plot. Price readouts use candle closes, spread readouts use basis
points, and volatility readouts include window coverage. Screen readers retain
latest values and unavailable venue names through each chart's availability note.

Only full rolling windows are plotted. Missing or invalid closes leave gaps;
separate line segments prevent interpolation across them. Crosshair readouts
show coverage and display partial windows as unavailable, not zero; availability
notes also flag incomplete latest windows. Valid flat-price windows show zero
volatility.

Market charts resize with the selected asset count: one chart is full-width
and tallest, two and three step down in height, and four retain the original
two-column desktop layout with 25rem cards (22rem on mobile). Smaller screens
keep charts stacked. Heights use viewport-aware bounds, and chart canvases
automatically resize when assets are added/removed or panels are expanded.

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
