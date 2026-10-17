# overseer

Market-data collection + dashboard for crypto venues (Binance, Bybit,
Hyperliquid, Lighter, Extended). Two independent processes sharing one
TimescaleDB:

* **scheduler** — async ingest: OHLCV bars, settled funding, OI/volume and
  top-of-book snapshots, plus tracked-address fills, per `symbols.toml`.
* **web** — Flask public analytics dashboard (price, bid–ask spread, and
  reported-volume/open-interest ratio charts; spot-perp basis; funding; flow;
  wallets), an anonymous browser-local Positions tool and an internal health
  page. Persistent market-data collection runs without browsers or web workers;
  account positions are read only while the Positions tool is open.

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

Zcash (`ZEC`) is configured for Binance spot and perpetuals, Bybit perpetuals,
Hyperliquid, Lighter, Extended, RISEx, BULK and Bullet. Bybit's public spot
catalog had no ZEC listing when checked on 2026-10-16, so no spot mapping is
configured there. Restart the scheduler and web workers after changing the
registry; historical chart series become available as collection writes data.

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

# Optional dashboard login (not needed for Positions or public analytics):
uv run flask --app web:create_app create-user you@example.com
# Add --internal only if the account also needs /health access.
```

## Browser-local Positions tool

Open **Tools → Positions** without signing in. Enter a wallet address and shared
label, then choose exchange checkboxes. Hyperliquid and RISEx are selected by
default; EVM wallets can also select Lighter. Bulk requires a Solana public key.
Submitting discovers exchange-reported owned accounts automatically, follows
pagination, and adds each account separately without asking for subaccount IDs.
The main account keeps the shared label; subaccounts use exchange names or
generated labels with short identities. Existing venue/address pairs are skipped
without changing their identifiers, labels or tokens. Re-add a saved wallet to
discover newly created or previously unwatched subaccounts.

Discovery completes and commits independently for each selected exchange:
a failed or cancelled lookup adds none of that exchange's pending accounts,
without undoing completed exchanges. The tab supports 32 accounts. A discovery
that cannot fit is rejected explicitly rather than silently truncated or
partially added. Coverage notes identify exchanges that need attention.

Watchlist and add/edit controls sit above funding controls and portfolio totals.
Only the account strip is sticky: **Add** and **Manage** remain available while
scrolling. At the top, flat entries retain labels, exchange names, compact
identities and reading quality, with subtle separators instead of boxed tiles.
Once pinned, the strip collapses to one slim line of exchange logos and shortened
identities, account count and a compact needs-attention count. It re-expands at
the top, while Add/Manage is open, or when an account tile receives keyboard focus.
The expanded layout places the scrollable account row underneath the controls on
narrow screens. Reserved layout space prevents collapse/expansion from shifting
portfolio content; an eight-pixel scroll deadband prevents threshold flicker.
Reduced-motion preferences disable the brief logo fade; height is never animated.
Wallets and long indices show the first/last three characters; `0x` prefixes
are retained and indices of six digits or fewer stay intact. Full identities
are available on hover, keyboard inspection and in account details.
Abbreviations are not unique:
selection uses the account's local identifier, never its shortened address.
Selecting a tile opens that account's details without filtering portfolio totals.
**Manage** opens the framed account list with separate positions/funding states.
Expand an account for full identity, timestamps, coverage, balances and
editing/token/removal controls. Matching labels never merge distinct accounts.
Privacy details and **Clear tab watchlist** sit inside Manage. **Add** opens the
entry form; it opens automatically when empty and closes after a successful add
or edit. Restored non-empty tabs start with both management and entry closed.
Pending, partial, delayed, stale and failed readings remain explicit in expanded
entries and full hover/focus descriptions. Compact entries mark each affected
account with an outlined `!` badge spaced away from its identity, and separate
the attention count in its own outlined badge. Warnings do not rely on color
alone.

Exchange marks are vendored from the official
[Hyperliquid brand kit](https://hyperliquid.gitbook.io/hyperliquid-docs/brand-kit),
[RISEx website](https://www.rise.trade/en),
[Lighter brand kit](https://lighter.xyz/brand-kit) and
[Bulk wordmark](https://www.bulk.trade/assets/bulkx-logo-light.svg).
Icons are served locally, without browser requests to external logo hosts.

Each independently opened tab has its own watchlist. Addresses and labels live
in `sessionStorage`, survive refreshes, and normally disappear when the tab
session ends. Browser session restoration or tab duplication can retain a copy.
**Manage → Clear tab watchlist** removes the tab's list. No positions watchlist, account
snapshot, funding history or read-only token is written to the database.

Read-only tokens, snapshots and funding pagination live only in page memory:
refreshing discards them and starts fresh reads for the retained watchlist.
Re-enter an optional Lighter token after refresh. Browser position API requests
omit login cookies and do not include a login/email identifier.
The server still processes addresses and optional tokens transiently to query
exchanges. Only public market metadata and current FX are cached server-side,
not account results. The page and API use `no-store`, preventing personal
results from entering the public analytics cache. Browser storage is
not protection against malicious same-origin JavaScript or a shared browser
profile; operators must not log request bodies or retain them in tracing.

Account discovery uses the anonymous `POST /api/positions/accounts` endpoint
without read-only tokens, signatures or exchange writes:

- **Hyperliquid:** the master wallet's
  [`subAccounts` info query](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
  supplies owned account addresses and names. The master is included; vaults
  and agent wallets are not added as owned subaccounts.
- **Lighter:** unsigned
  [accountsByL1Address](https://apidocs.lighter.xyz/reference/accountsbyl1address)
  reads discover all returned indices, including inactive accounts, across
  every page. A known numeric index also works directly, but includes only that
  account, not the wallet's siblings.
- **Bulk:** the master public key's
  [full account response](https://docs.bulk.trade/api-reference/getAccount)
  supplies separate owned subaccounts. Attached isolated positions already
  belong to their parent account's readings and are included once, not added
  as duplicate accounts.
- **RISEx:** the [public API](https://developer.rise.trade/llms.txt) exposes no
  wallet-to-subaccount listing. Only the entered account address is included;
  the form displays that discovery limitation rather than claiming full wallet
  coverage.

Discovery runs on submission, not keystrokes or regular financial refreshes.
Changing the form, cancelling, removing accounts, clearing the watchlist or
leaving the page invalidates pending results so they cannot restore removed
accounts. Address/venue edits still change one saved account, not its siblings.
An optional Lighter single-account token is applied only to its matching
discovered index; other discovered accounts remain public reads with explicit
private-funding coverage warnings. An all-account token applies to newly added
Lighter accounts. Neither token scope changes existing accounts on re-add.

While open, the browser targets positions every 60 seconds and settled funding
every 5 minutes, continuing bounded history pages between full refreshes.
Reads share a 60-request/minute budget per web worker and bounded concurrency.
Slow venues or many accounts can delay these targets. Refresh requests actual
venue reads, not saved database snapshots; there is no off-tab collector or
durable catch-up. Failed reads retain the last good data only in page memory.
Pending, delayed, stale, partial history and errors remain explicit; incomplete
portfolio totals are unavailable rather than treated as zero. Repeated pages
are rejected rather than double-counted. An offline screen stays marked
retained as it ages; funding-period changes hide the old period's amounts.

A small indicator below **Fetch fresh readings** shows the oldest successful
positions and funding read independently, among watched accounts with a read
timestamp. Unread accounts and errors remain explicit rather than disappearing
behind a newer account's age. Ages advance with the page clock; failed attempts
and summary recalculations do not reset them. Retained data and incomplete
funding scans are labelled, and scans without a completed read timestamp do not
invent an age. Read freshness does not imply complete funding coverage.
Changing the funding period resets its read age; clearing the watchlist removes
the indicator. The compact sticky strip remains unchanged.

Collapsed coin rows surface direction, net USD delta, native settled funding,
native unrealized P&L and reading quality. Blue indicates long/positive delta;
purple indicates short/negative delta, not loss. Narrow screens use two scan
bands: position above financial results.

Expanded coin tables align venue/account, side, base size, entry/mark prices,
native P&L, settled funding and freshness. Secondary totals and USD valuation
are in the `Coin totals & valuation` disclosure. Scope, collateral and coverage
details remain in disclosures rather than repeated paragraphs. Shortened
amounts use
`≈`; tiny nonzero amounts use scientific notation. Tap an amount or focus it and
press Enter to see its exact supplied value. Display shortening does not alter
stored or aggregated amounts.
On narrow screens, the table scrolls within the coin row rather than widening
the whole page.

Summary and coin-row settled funding and unrealized P&L values are green for
positive and red for negative. Each native currency bucket keeps its own sign
colour. Portfolio unmatched exposure, zero and unavailable values remain
neutral; incomplete coverage and retained readings remain explicitly labelled.

Exposure nets longs and shorts **within each asset**, never across different
coins. Gross unmatched USD exposure sums absolute per-asset deltas using a
common current reference price. Token balances and collateral holdings remain
separate from perpetual hedge exposure. Quote, settlement, eligible collateral
and P&L currencies are shown independently.

Funding periods are 24 hours, 7 days and 30 days: positive means received,
negative means paid. Only settled regular funding is counted; RISEx unsettled
accruals and dividends are excluded. Native currency buckets are preserved.
RISEx funding is attributed to account + market, not a guessed historical
cross/isolated margin mode. USD figures use fresh Coinbase **current** FX,
not payment-time FX; rates older than five minutes or missing rates make the
estimate unavailable, never imply a stablecoin peg.
Bulk reports native USD funding against USDC settlement/collateral; its
undocumented margin currency is shown as `UNSPECIFIED`, not guessed.

Hyperliquid, Bulk and RISEx use unsigned public account reads. Private Lighter
funding needs an official `ro:…` token created at
<https://app.lighter.xyz/read-only-tokens/>. Trading keys, signers and private
keys are rejected. Tokens can be replaced or cleared on the account card and
are never returned by the API. No server encryption key is needed because
there is no token persistence. There is no signing, trading or exchange mutation.

### Existing database cutover

Stop the old scheduler and web workers before applying
`migrations/011_browser_positions.sql`. **This irreversible migration removes
all saved position accounts, labels, encrypted tokens, snapshots and account
funding events**, plus the obsolete position FX table. Login accounts and
unrelated market data remain intact. Old backups, WAL archives and logs are
not erased by dropping tables; review their retention/access separately.

Then run `uv sync --frozen`, rebuild CSS with `npm run css`, rebuild/restart
the scheduler and web service, and install the updated Gunicorn/Nginx configs.
The web uses threaded workers for bounded venue reads; only the aggregation
endpoint accepts larger bodies for browser-held funding history. Positions
API request/response buffering and proxy caching are disabled so large bodies
do not spool to Nginx temporary files. Remove the
obsolete `OVERSEER_POSITION_TOKEN_KEY` deployment secret. Fresh databases apply
migrations in order, including the purge. Mock mode still generates only market
analytics: Positions makes real exchange reads and never invents holdings.

Regression checks, including the PostgreSQL purge boundary:

```sh
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/overseer_test \
  uv run --extra dev pytest -q
```

Use an isolated test database; the purge case creates and removes its own
schema. Without `TEST_DATABASE_URL`, that database case is skipped.

## Execution estimator

Open **Tools → Execution estimates** (`/execution`) without signing in. Choose a
crypto coin and buy/sell direction; the table compares configured perpetual
venues at **$10k, $100k, $1m and $10m**. Spot and traditional-asset markets are not
mixed into this comparison. This is a read-only estimator, not an order router.

The four **Best venue by trade size** cards lead with the best captured venue
and its fee-adjusted **USD price per one coin**, not the total trade cost.
Winner cards and table prices show **total trading cost in bps (fee + slippage)**
beneath the price. A visible one-way fee-plus-book-slippage legend sits above
the five-column table. Effective prices are explicitly labelled USD per coin,
after taker fees; before-fee and native FX-unknown exceptions remain marked.
Hover or keyboard-focus a price in the table or a winner card for its venue's
**OB best ask/bid**, book slippage and gross fill, taker fee rate and
fee-adjusted USD price. Per-coin fees and the repeated cost total stay in the
expanded breakdown, not the hover. Native quote and USD
values stay distinct when FX conversion applies. Rounded display values carry
`≈`; accessible text and the expanded breakdown retain exact captured values.
The tooltip stays open when the pointer moves onto it; **Escape** dismisses it.
The popup prefers below the price and flips above when space is limited.
Click, tap or press Enter/Space on a price for native gross → USD gross →
fee-adjusted USD values, followed by the grouped fee + slippage = total equation
and exact component values. Touch uses this disclosure without a hover overlay.
Long cost assumptions sit in a nested disclosure below the numeric breakdown.
Expand **Details** for native accounting, provider diagnostics, timestamps,
depth and order limits; reference FX remains under **How estimates work & quote details**.
On mobile, a swipe hint identifies the horizontally scrolling size columns;
venue names stay pinned.
Collapsed venue rows keep the name, fee, quote age and short availability status
visible, with **Details** alongside the fee/age metadata. Full diagnostics and
book notes expand below without fixed-height clipping. Winner cards use compact
spacing while preserving full venue names and readable prices.

Each refresh captures public REST depth once per venue, then walks asks for buys
or bids for sells. A median fresh USD midpoint sets the same unrounded base
quantity at every venue for each size. The sizes are reference notionals, not
exact all-in cash budgets. Effective average prices add one-way buy taker fees
or subtract sell taker fees; **Best** selects the lowest buy or highest
sell price among eligible complete snapshots.

Total trading cost bps are the **selected taker fee bps + visible-book slippage
bps**: a 6 bps fee plus 4 bps slippage displays **10 bps total**. Buy slippage
uses `(average native fill / venue's best ask - 1) × 10,000`; sell slippage uses
`(1 - average native fill / venue's best bid) × 10,000`. This is an additive
bps summary, not the fee-adjusted price's percentage difference from a common
benchmark. It excludes spread and cross-venue price premiums/discounts.
Fee-adjusted USD prices and **Best** rankings remain separate and unchanged.
Zero is valid; tiny nonzero values rounded to zero are marked approximately
zero, with exact costs in the expanded price breakdown.

The API's `all_in_cost_bps` carries this fee-plus-slippage total as an exact
Decimal string, or null when fees, the reference quantity or a full eligible
quote are unavailable. Unknown fees show **Total cost unknown**; shallow,
capped and unavailable sizes show no numeric cost. Bps do not require FX:
complete native quotes can show total cost while their USD price is unknown
and they remain unranked. Expired table readings retain a dimmed captured cost,
while winner-card costs clear. This is a one-way execution estimate, not every
trading/holding cost: funding, settlement, gas, latency and account-specific
extras are excluded.

Eligible full, fresh, uncapped estimates also expose exact Decimal strings
`top_of_book_price` (native best ask for buys, best bid for sells),
`top_of_book_price_usd`, `fee_per_coin` (native average fill × selected taker
fee rate) and `fee_per_coin_usd`. These fee fields are **per coin**;
`fee_usd` remains the **whole-order** fee. Unknown fees leave both per-coin fee
fields null; unknown FX leaves the native values intact and the USD fields
null. Ineligible or unavailable estimates return null for all four fields.

Base taker fees are selected initially. Open **Fees & VIP tiers** to choose each
venue's published native VIP/volume tier or account type; selections remain in
page memory only.
Official sources, verification dates and account/product caveats are available
in the fee disclosure. RISEx's anonymous numeric fee schedule is unverified:
its complete estimates are explicitly **before fees** and never ranked as free.
HIP-3 market-specific fees likewise must not inherit native Hyperliquid rates.

Initial load, coin/direction/fee changes and **Refresh prices** request new
books through the uncached `POST /api/execution` endpoint. The visible page also
refreshes automatically every **60 seconds**, with a countdown. Manual and
settings changes reset that deadline; automatic captures never overlap an
in-flight request. Hidden pages pause, and returning after a missed deadline
requests one capture rather than replaying every missed minute. Failed captures
can recover on the next scheduled refresh.
There is no new WebSocket feed, collector or database depth persistence.
`OVERSEER_MOCK=1` does not fabricate these execution books; it only mocks the
stored analytics. The reader admits two captures per worker and at most four
venue reads concurrently, with an **8-second per-venue timeout** and **20-second
capture deadline**. HTTP connect/read timeouts are 4/8 seconds. There are no
automatic transport retries. Healthy venues remain available when siblings
fail; public diagnostics distinguish timeouts, HTTP/rate-limit responses and
network failures without exposing upstream response bodies.

Snapshot ages distinguish venue source time from receipt time and advance
locally. After ten seconds, retained prices are marked expired and lose their best
highlight; winner cards clear even before the next minute refresh. Refresh
manually for a new comparison sooner.
An open table tooltip labels retained values **Expired — not a current quote**;
an expired winner-card tooltip closes with its cleared card. Coin, direction,
fee and capture changes dismiss old tooltips, as do hidden or departing pages.
Execution uses only its own quote-age
status and countdown: the unrelated ingested-bar Market feed strip and warning
banner are omitted on this page. Insufficient visible depth,
published market-order size limits, unavailable providers and unknown FX remain
explicit, without last-level extrapolation or ticker-price fallbacks.

**Snapshot too shallow** means the captured slice cannot cover the requested
base quantity, not that the venue's entire book is illiquid. The cell shows the
percentage captured and its price-level count in the tooltip.
Hyperliquid's full-precision public `l2Book` response contains at most **20 levels
per side**; it can omit liquidity needed for a large trade even when the venue
frontend reports low slippage. Its `nSigFigs`/`mantissa` options expose broader
price-grouped depth, but bucket prices introduce rounding uncertainty.
This estimator currently uses full-precision books only; grouped prices are not
silently substituted or ranked as exact fills. See the
[official L2 snapshot contract](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#l2-book-snapshot).

USD valuation uses timestamped Coinbase reference FX, cached for at most thirty
seconds; stablecoins are never silently pegged to USD. This is not an executed
currency conversion. Venue notes explain price accounting versus settlement,
source-time limitations and quantity assumptions, including Bullet's explicitly
marked Binance-FAPI-compatible base-size inference. Current Bullet REST depth
allows 1000 levels, unlike the older SDK's 25-level description:
<https://tradingapi.bullet.xyz/docs/rest>.

Quotes are unsynchronized, non-firm and not an order-acceptance guarantee.
Funding, lot rounding, quanto settlement economics, staking/referral/promotional
discounts, builder charges and account-specific margin/fee overrides are
excluded. Public market-order maximums are honored when available.

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
The Execution estimates page omits this historical-feed indicator and uses
its independently captured book ages, expiry status and refresh countdown.

Navigation keeps Charts, Basis, and Funding as primary pages. Tools opens a
disclosure containing Trade flow, Tracked wallets, Positions and Execution
estimates; each retains its direct URL and independent controls. Tools is
highlighted on these pages. The disclosure supports normal Tab navigation,
arrow-key entry, Escape, and outside/focus-leave dismissal; on mobile, Escape
closes Tools before closing the main menu.

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

Below 640px, chart filters, rolling-window controls and venue chips sit in a
native **Filters** disclosure, collapsed initially. Its visible summary shows
the active market, asset or asset count, metric/window, history and selected
venue count. Changing selections leaves it open; saved selections still
survive reloads. Desktop keeps the original controls fully visible. Crossing
the breakpoint preserves settings and keeps focused controls reachable;
closing the mobile disclosure dismisses its pickers. The chart is visible
without first scrolling past the full filter panel.

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
