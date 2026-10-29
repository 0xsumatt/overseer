# overseer

Market-data collection + dashboard for crypto venues (Binance, Bybit,
Hyperliquid, Lighter, Extended). Two independent processes sharing one
TimescaleDB:

* **scheduler** — async ingest: OHLCV bars, settled funding, OI/volume and
  top-of-book snapshots, plus tracked-address fills, per `symbols.toml`, and
  five-minute execution samples and order-book depth for BTC, ETH and SOL.
* **web** — Flask public analytics dashboard (price, bid–ask spread, and
  reported-volume/open-interest ratio charts; spot-perp basis; funding; flow;
  wallets; order-book depth heatmaps), execution-cost and margin calculators,
  an anonymous browser-local Positions tool and an internal health
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
without treating them as separate physical exchanges. Every venue has one
display name, used on every page: `VENUE_NAMES` in `src/core/symbols.py`
(Binance, Bybit, Hyperliquid, Lighter, Extended, BULK, RISEx, Bullet), plus
the `hip3:` prefix and configured deployer name for HIP-3 markets, e.g.
`hip3:tradexyz` and `hip3:entropy`. Pages receive the map as `VENUE_LABELS`
(`labelFor()` in `venues.js`). A fee schedule's product name, such as
"Binance USD-M (USDT)", appears only as a tooltip and in fee settings. Names
do not change venue identifiers, data lookups, or saved exchange selections.
The Funding markets table never scrolls sideways, however many venues are
configured. Columns size to their content; when they get tight the table
switches to a compact density (one-decimal APR), and when even that cannot fit,
each asset becomes a block listing its venues in a wrapping grid with its APR
spread. Long deployer names break only after `hip3:`, never inside a word.
Rows scroll vertically inside the card, which always has room for at least
eight table rows (`MARKETS_MIN_ROWS` in `funding.html`). Where the screen
allows, the card ends just above the fold and shows more; on shorter screens it
keeps eight rows and runs past the fold. The stacked list gets about 640px of
blocks. A fade marks the bottom edge while more rows remain.
Set `orderbook = true` on a venue to retain one best-bid/ask snapshot per
configured market every 60 seconds. Override that cadence with
`orderbook_poll_seconds`.

Funding rates are stored as what one settlement charges, with that settlement's
interval, so APR = rate × 8760 ÷ interval hours. Venues that quote an 8-hour
rate but settle hourly are converted when collected: Lighter's current rate,
and every Bullet rate, since Bullet charges an eighth of its quoted rate each
hour (<https://docs.bullet.xyz/exchange/trading/funding>). A database that
collected Bullet before that conversion needs
`migrations/014_bullet_funding_basis.sql` once, applied with the scheduler
stopped, before it restarts; applying it again changes nothing.

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

The status bar under the navigation (**Readings**) shows the oldest successful
positions and funding read independently, among watched accounts with a read
timestamp, with **[refresh]** to request fresh readings now and the refresh
cadence beside them. Unread accounts and errors
remain explicit rather than disappearing behind a newer account's age. Ages
advance with the page clock; failed attempts and summary recalculations do not
reset them. Retained data and incomplete funding scans are labelled, and scans
without a completed read timestamp do not invent an age. Read freshness does
not imply complete funding coverage. Changing the funding period resets its
read age; an empty or cleared watchlist shows **No accounts**. The compact
sticky strip remains unchanged.

Collapsed coin rows surface direction, net USD delta, native settled funding,
native unrealised P&L and reading quality. Blue indicates long/positive delta;
purple indicates short/negative delta, not loss. Narrow screens use two scan
bands: position above financial results.

Expanded coin tables align venue/account, side, base size, entry/mark prices,
native P&L, settled funding and freshness. Secondary totals and USD valuation
are in the `Asset totals & valuation` disclosure. Scope, collateral and coverage
details remain in disclosures rather than repeated paragraphs. Shortened
amounts use `~`; tiny nonzero amounts use scientific notation. Tap an amount or
focus it and press Enter to see its exact supplied value. Display shortening
does not alter stored or aggregated amounts.
On narrow screens, the table scrolls within the coin row rather than widening
the whole page.

Summary and coin-row settled funding and unrealised P&L values are green for
positive and red for negative. Each native currency bucket keeps its own sign
colour. Portfolio unmatched exposure, zero and unavailable values remain
neutral; incomplete coverage and retained readings remain explicitly labelled.

Exposure nets longs and shorts **within each asset**, never across different
coins. Gross unmatched USD exposure sums absolute per-asset deltas using a
common current reference price. Token balances and collateral holdings remain
separate from perpetual hedge exposure. Quote, settlement, eligible collateral
and P&L currencies are shown independently.

Funding periods are 24h, 7d and 30d: positive means received,
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
crypto coin, a trade (**Buy**, **Sell**, or a round trip: **Buy, then sell** or
**Sell, then buy**) and an optional **Hold**; the table compares configured
perpetual venues at **$10k, $100k, $1m and $10m**. Spot and traditional-asset
markets are not mixed into this comparison. This is a read-only estimator, not
an order router.

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
values stay distinct when FX conversion applies. Hover values are rounded (the
popup says so); accessible text and the expanded breakdown retain exact
captured values, written out in full with grouped digits and no exponent
notation.
The tooltip stays open when the pointer moves onto it; **Escape** dismisses it.
The popup prefers below the price and flips above when space is limited.
Click, tap or press Enter/Space on a price for native gross → USD gross →
fee-adjusted USD values, followed by the grouped fee + slippage = total equation
and exact component values. Touch uses this disclosure without a hover overlay.
A winner card's breakdown opens as a panel below the cards, so the cards keep
their height; one is open at a time, and Escape, **close** or a click outside
dismisses it.
Long cost assumptions sit in a nested disclosure below the numeric breakdown.
Expand **Details** for native accounting, provider diagnostics, timestamps,
depth and order limits; reference FX and methodology sit under **How this works**.
On mobile, a swipe hint identifies the horizontally scrolling size columns;
venue names stay pinned.
Rows run best first at one size: cheapest to buy, highest to sell or cheapest
round trip, by the same fee-adjusted value that picks **Best**. Venues that can't
be ranked at that size (shallow book, above a venue limit, unknown fee,
unavailable) follow in name order. The size whose header shows ↓ sets the
order, `$10K` by default; select another size's header to reorder, and the
choice is remembered in the browser. Rows re-sort with every capture.
Collapsed venue rows keep the name, fee, the hold's funding (when one is
chosen), quote age and short availability status visible, with **Details**
alongside that metadata. Full diagnostics and
book notes expand below without fixed-height clipping. Prices use one precision
per comparison, about five significant figures at the coin's price and never
fewer than two decimals; the underline affordance appears only on hover, focus
or an open breakdown. The first capture shows placeholders, not repeated
loading text. On phones, winner-card prices stay on one line.

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
Zero is valid; tiny nonzero values that round to zero show as `<0.01 bps`,
with exact costs in the expanded price breakdown.

The API's `all_in_cost_bps` carries this fee-plus-slippage total as an exact
Decimal string, or null when fees, the reference quantity or a full eligible
quote are unavailable. Unknown fees show **Total cost unknown**; shallow,
capped and unavailable sizes show no numeric cost. Bps do not require FX:
complete native quotes can show total cost while their USD price is unknown
and they remain unranked. Expired table readings retain a dimmed captured cost,
while winner-card costs clear. Without a hold this is a one-way execution
estimate: funding, settlement, gas, latency and account-specific extras are
excluded.

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
in the fee disclosure. RISEx's tiers come from its published fee page (rolling
14-day volume, 3.00 down to 1.50 bps taker), since its account fee lookup needs
a JWT. A venue whose fee is unknown shows its estimates **before fees** and is
never ranked as free; HIP-3 market-specific fees likewise must not inherit
native Hyperliquid rates.

**Buy, then sell** and **Sell, then buy** price a round trip: the same base
quantity bought and sold against one capture. Cells and winner cards then show
the whole trade's cost in USD, with that cost in bps of the trade size beneath.
It is the fee-adjusted buy minus the fee-adjusted sell, so the spread, slippage
on both sides and both taker fees count while a venue's price level cancels
out; **Best** is the lowest total. It assumes you close into a book like
today's. A round trip is as shallow as its shorter side, and a venue without a
known fee shows only its spread and slippage, labelled **before fees** and
never ranked. Hovering shows each leg's fee-adjusted fill, the spread and
slippage, both fees and any funding. The API returns `total_cost_usd` and
`total_cost_bps`, their parts (`spread_slippage_usd`, `fees_usd`,
`funding_usd`) and each leg's one-way estimate under `buy` and `sell`.

**Hold** (8h, 24h, 7d or 30d) adds the funding the position pays
or receives on its opening leg: a buy holds a long and a sell a short, and
positive funding means longs pay shorts. Holds of up to a day use each venue's
current (next-settlement) rate pro rata, which must be no older than 15
minutes. Longer holds repeat the previous 24 hours of settled funding instead,
so one live rate is not stretched over a week or a month; that day is averaged
per hour and needs settlements covering at least 12 of its 24 hours. Averaging
also keeps BULK, whose collector stores its predicted hourly rate every 15
minutes, from counting four times over. One-way prices then include funding
(paying it raises a buy's price and lowers a sell's; receiving it does the
opposite), the cost line adds it to fee and slippage, and each venue row shows
the hold's funding in bps. A venue without usable funding shows **Funding
unknown** and its prices before funding, and is not ranked; a failed read of
the funding tables does the same for every venue rather than treating funding
as zero. Neither rule predicts future rates. The request reads the `liquidity`
(current) or `funding_rates` (settled) table only when a hold is chosen.

Initial load, asset, trade, hold and fee changes and **[refresh]** (in the
status bar, beside the capture time) request new books through the uncached
`POST /api/execution` endpoint. The visible page also refreshes automatically
every **60 seconds**, with a countdown; **[refresh]** dims while a capture
runs. Manual and settings changes reset that deadline. A page never has two requests in flight:
the server keeps capturing after a fetch is abandoned, so a change made while a
capture runs is captured once that request settles, and several quick changes
become one capture. Hidden pages pause, and returning after a missed deadline
requests one capture rather than replaying every missed minute. Failed captures
can recover on the next scheduled refresh.
The live comparison has no WebSocket feed and never reads stored books; with a
hold it reads stored funding rates. Only the scheduled history below keeps
anything from the books, as per-venue results rather than books.
`OVERSEER_MOCK=1` does not fabricate these execution books; it only mocks the
stored analytics, including that history and funding rates. The reader admits
two captures per
worker and at most four venue reads concurrently, with an **8-second per-venue
timeout** and **20-second
capture deadline**. HTTP connect/read timeouts are 4/8 seconds. There are no
automatic transport retries. Healthy venues remain available when siblings
fail; public diagnostics distinguish timeouts, HTTP/rate-limit responses and
network failures without exposing upstream response bodies.

Snapshot ages distinguish venue source time from receipt time and advance
locally. After ten seconds, retained prices are marked expired and lose their best
highlight. Winner cards keep the last winner, dimmed, with a ticking **Expired**
badge until a new capture replaces it, so the comparison never goes blank between
minute refreshes. Refresh manually for a current comparison sooner.
Open tooltips and breakdowns mark retained table and winner-card values as
expired: the last capture, not a current quote. Asset, trade, hold,
fee and capture changes dismiss old tooltips, as do hidden or departing pages.
On Execution the status bar shows **Order books**: how many venue books the
last capture got, its UTC time, a Fresh / Expired state and the refresh
countdown, instead of the unrelated ingested-bar market feed. Routine expiry
shows only there and on the cards; a warning notice appears only when a
refresh fails or a capture is unusable. Insufficient visible depth,
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
Funding counts only when a hold is chosen. Lot rounding, quanto settlement
economics, staking/referral/promotional discounts, builder charges and
account-specific margin/fee overrides are excluded. Public market-order maximums
are honored when available.

### Cheapest venue history

Below the live comparison, **Cheapest venue history** shows which venue is
usually cheapest for BTC, ETH and SOL. The scheduler captures every compared
venue's public book for each coin every **5 minutes** on its own clock, so a
share means "of the scheduled captures", not "of the times someone had the page
open". Each capture stores, per venue, side and size (**$10k, $100k and $1m**),
the pre-fee average USD fill, or why the venue could not be compared, in
`execution_samples` (migration 012). Eligibility is exactly the live
estimator's: ten-second freshness, captured depth, published order limits and
reference FX. $10m is omitted: Hyperliquid's 20-level public book reads
**Snapshot too shallow** at that size, so a ranking would reflect which books
are visible rather than which venue is cheapest.

Fees are applied when the history is read: base taker tiers by default, or the
tiers chosen under **Fees & VIP tiers**. A fee-schedule change therefore never
rewrites stored results. The history follows the page's trade: buys, sells or
round trips, where each venue's buy and sell from the same capture are paired.
Both round-trip choices show the same history, since without funding they cost
the same; the history never includes funding. Each size card covers the last 7
or 30 days:

* **Cheapest**: share of comparisons in which the venue had the best
  fee-adjusted price, or for round trips the lowest round-trip cost; ties
  split. A comparison needs at least two venues with a complete, fresh quote
  and a known fee.
* **Gap, bps**: median extra cost versus the cheapest venue, when this venue
  was not cheapest.
* **Quoted**: share of captures with a complete, fresh quote, on both sides for
  a round trip; hover for why the rest were not comparable.

Rows use each venue's short name (the full product label is the tooltip), and
the summary line shows the latest capture in UTC, turning amber once three
scheduled captures in a row are missing. Cards sit side by side from 1280px
wide and stack below that.

Venues without a known fee are listed as **Not
ranked**. The coin list lives in `src/core/execution_history.py`; a coin needs
at least two compared venues in `symbols.toml` to be scheduled. Coins are
captured one after another, each recorded in `job_runs` as `execution:<coin>`,
under the same reader limits as the page. A capture in which no venue returned
a book is recorded as a failure and stores nothing: our network is a likelier
cause than every exchange at once. With eight venues that is roughly 24 book
requests every five minutes and about 41,500 rows a day, compressed after seven
days.

`GET /api/execution-history?asset=BTC&side=buy&days=7` serves the statistics;
`side` is buy, sell or round_trip, `days` is 7 or 30, and
`&fees=bybit_perp:vip_1,…` selects tiers. Responses are
cached for 30 seconds like the other public analytics. Apply migration 012
before restarting the scheduler: without the table every history run fails.

## Depth heatmap

Open **Tools → Depth heatmap** (`/depth`) without signing in. It shows resting
order-book size near the price over time for each coin and venue in the
cheapest-venue history.

The scheduler's five-minute execution capture already downloads every venue's
public book, so depth needs no extra exchange requests. Each usable book is
stored in `depth_snapshots` (migration 013) as resting base quantity in 1 bps
buckets out to ±100 bps from that book's own mid, with how far the book itself
reached on each side, its level counts, quote currency, USD rate and age.
Books with an empty side or crossed or unsorted levels are skipped, as are
books more than a minute old at capture. Rows record their source (`rest`) and
bucket width, so a finer feed, such as a full WebSocket book, can share the
table.

Reach is the main limit of REST snapshots. On one live capture, Binance and
Bybit BTC books reached only about ±16–28 bps despite 1,000 levels a side, and
Hyperliquid's 20 levels about ±2.4 bps, while SOL on Binance and Bybit filled
the whole ±100 bps window. Beyond a side's reach the book showed nothing, which
the page draws differently from "nothing resting".

Each stored book is a column: blue bids below the mid, purple asks above,
brighter for more size in that 1 bps band (square-root scale, full colour at
the window's 95th percentile), over a faint band for the part of the book the
snapshot showed. **Around mid**, the default, plots distance from each
snapshot's mid in bps, so a narrow book still fills the chart; **Price** plots
price levels, so orders resting at a fixed price line up across time. Hovering
names the size in one bucket, or why there is none. Windows are 6h or 24h
(every snapshot), 7d (one per 30 minutes) or 30d (one per 2 hours);
long windows take the latest real snapshot in each slot rather than averaging,
because buckets move with the mid. A missing snapshot stays a visible gap.
Asset, venue, window and view are remembered in the browser.

`GET /api/depth-heatmap?asset=BTC&venue=binance_perp&hours=24` serves the
columns; `hours` is 6, 24, 168 or 720. Sizes are base units to four
significant figures, and each side's array stops at its last non-empty bucket.
Responses are cached for 30 seconds like the other public analytics.

Storage is about 6 MB a day before compression for three coins on eight venues
(two 100-value `real` arrays per book, 24 books every five minutes), compressed
after seven days. Apply migration 013 before restarting the scheduler: without
the table each execution capture reports a failure, although its samples are
still written.

## Margin calculator

Open **Tools → Margin calculator** (`/margin`) without signing in. Choose a
coin, **Long** or **Short**, a size in USD (`100000`, `100k` and `1m` all
work), the price move the position must **Survive** (10% by default) and a
**Hold**, and optionally a yearly return on idle capital. A **Second position**
in another coin turns it into a pair trade held on the same venue. Each
calculation captures every compared venue's public book and margin rules, so it
takes a few seconds per coin; settings and fee tiers work as on Execution
estimates and are remembered in the browser. Changing any setting recalculates;
the status bar (**Order books**) shows when the last calculation captured its
quotes and how many venues it ranked, with **[refresh]** to recalculate on fresh
books;
the winning row is tinted and marked **best** beside the venue name. Column
definitions and caveats live under **How this works**.

At the same leverage every venue asks for the same margin (size ÷ leverage), so
margin alone cannot make one venue cheaper. The calculator compares venues at
the same safety instead: each must keep the position open through your move.
Margin here is cash margin in USDC or USDT: no spot collateral, borrowing or
account discounts. Per venue the table shows:

* **Max leverage** the venue allows at that size (venues cap leverage by size).
* **Capital needed**: the loss at your move plus the venue's maintenance margin
  at the moved price, and never less than the venue's minimum, size ÷ its
  maximum leverage (marked **leverage cap** when that minimum sets it). A
  higher maintenance rate or a lower cap means more capital for the same
  safety. For a pair it also shows the difference against each position held
  on its own.
* **Leverage**: size ÷ capital needed.
* **Maintenance**: the venue's maintenance margin at entry.
* **Trading** (spread, slippage and both taker fees to open and close, from the
  live books as on Execution estimates), **Funding** over the hold (the same
  rule: current rate up to a day, previous 24 hours of settled funding beyond),
  **Capital cost** (capital needed × yearly rate × hold, nothing until you
  enter a rate) and their **Total**. Costs are amounts paid: funding the
  position receives is shown as "received", and so is a total that received
  funding outweighs, never as a negative cost.

**Compare** ranks by the lowest total (the default) or by the least capital
needed. Only venues with a complete quote, a known fee and funding, and
published margin rules covering your size are ranked, so a venue with an unknown
fee is shown but never ranked. Details show each position's loss at the move,
maintenance before and after it, and, for one position, where it would be
liquidated with the capital needed posted in isolation, before fees and
funding: at your move, or further when a leverage cap sets the capital.

Margin's cost is usually small next to trading and funding: on one capture, the
$2,100 gap between Bybit's ($10,292) and RISEx's ($12,399) capital for a $100k
BTC long surviving 10% cost about $2 a week at 5% a year. It matters most where
a leverage cap binds: Bullet allows 2x on $1m of BTC, so it needs about $510k
where other venues need $104k–$124k, about $390 a week more at 5%.

| Venue | Margin rules from | Model |
|---|---|---|
| Binance | dated copy of the public Leverage & Margin page (`src/core/margin_tables.py`); its bracket API needs a key | brackets: notional × rate − maintenance amount |
| Bybit | `GET /v5/market/risk-limit` | risk-limit tiers with deductions |
| Hyperliquid | `metaAndAssetCtxs` margin tables | half the initial rate at each tier's maximum leverage, with deductions |
| Lighter | `orderBookDetails` margin fractions | flat: maximum 1 ÷ minimum initial fraction |
| Extended | market `riskFactorConfig` | brackets: risk factor is the minimum initial rate, maintenance half of it, whole position at its bracket's rate |
| RISEx | `/v1/markets` config | flat: maintenance = 1 ÷ maintenance factor |
| Bullet | `GET /fapi/v1/leverageBracket` (public) | Binance-style brackets |
| BULK | `GET /riskSurfaces` and `/ticker` | portfolio margin (below) |

Bybit and Bullet tables are cached for an hour and BULK's risk surfaces (about
0.7 MB a market) for ten minutes; the other venues' rules come with the market
details Execution already reads, refreshed each minute. Re-copy Binance's
brackets when `BINANCE_BRACKETS_CHECKED` grows old: Binance changes them without
notice, and caps new futures accounts at 20x for their first 30 days.
Maintenance follows the tier of the position's value at each price, as Binance
and Hyperliquid document, so a position that grows into a higher tier is
liquidated sooner: a $1m Bullet short with $200k after a 12% rise, not the
entry tier's 13.6%. Capital needed covers the worst point of the move, which
for a long falling through one of Extended's bracket edges is just above it,
where the higher bracket still applies.

**Pairs.** Both positions share one cross-margin account, and the test moves
each against you at once (long down, short up): severe for a hedge, but the same
test on every venue. Every venue except BULK adds the two maintenance margins:
Hyperliquid's portfolio margin, Bybit's per-coin risk units, Binance's portfolio
margin and the multi-asset modes of Lighter and Extended change what counts as
collateral or offset positions in the same coin, not a long BTC against a short
ETH. There a pair needs exactly what its legs need on their own, unless one
position's leverage cap leaves spare collateral the other can use. BULK
combines each position's maintenance with its published correlation between
the coins:

```
M = sqrt(M_1² + M_2² + 2 ρ M_1 M_2),   M_i = ±λ_i × notional_i (+ long, − short)
```

where λ is read from the coin's risk surface at the live volatility regime, by
position size and account leverage (gross exposure net of correlation ÷
capital). Because λ depends on the capital, the calculator finds the least
capital that covers the maintenance its own leverage implies. On one capture
(2026-10-20), $100k long BTC with $100k short ETH surviving 10% needed $21,589
on BULK against $23,997 for the two held on their own (−10%), while Hyperliquid
needed $23,348 either way. BULK's surfaces also raise λ for large positions at
low account leverage, and a hedge lowers that leverage, so with large legs the
pair can need more than its legs alone; the column marks either difference.
This follows BULK's published calculator; its engine's correlation discount
(`corr_discount` 0.9 in `/config?which=risk`) is undocumented and not applied.

`POST /api/margin` takes `{"position": {"asset", "direction", "size"}, "pair":
{…} or null, "move": "10" (percent, 0.1–100), "hold_hours", "rate", "rank":
"cost" | "capital", "fees"}` and returns each venue's legs, combined capital,
costs and ranking. Responses are never cached. The page needs no migration or
scheduler change.

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

Styles compile from `src/web/static/css/assets.css`, which imports `fonts.css`
(self-hosted IBM Plex Mono and Plex Sans), `base.css` (element defaults and the
colour tokens published as CSS variables), `components.css` (shared components)
and one partial per page under `css/pages/`. Rebuild `app.css` with
`npm run css` after template, JavaScript-class or style changes.

Every page has the same frame: navigation, a one-line status bar saying how
current the page's data is (`status_bar` macro), then the `page_header`,
filters in an unboxed `.toolbar`, the `page_status` slot and the content.
On phones, pages with four or more filters fold them behind one **Filters**
summary line (`filters` macro with `Overseer.filtersFold`); from 640px wide the
fold disappears. Sections are titled with `.section-label` (a short qualifier
goes inside it as `.section-count`) and panels with `.panel-header`; top-level
blocks sit 24px apart. Surfaces are separated by fill, not outline: an ink
page, opaque panel cards with a soft edge, and panel2 for raised or selected
things; dividers inside cards all use the same soft line. KPI figures sit in
one divided strip (`.kpi-grid`, or `.kpi-grid-3` / `.kpi-grid-2` for smaller
sets), not a box each, and every data page ends with a plain `[+]` **How this
works** row holding its explanations. Every container's text starts 12px in on
phones and 16px in from 640px wide, first table column included. Unknown
addresses and server errors show the same frame with a short message (JSON
for `/api/` routes), and the browser tab shows the amber cursor icon.

Type has one order: page title (sans), panel title (14px mono), section label
(spaced capitals between rules), field, KPI and table-header labels (spaced
capitals), data, then meta lines. Badges, bracketed panel controls and
disclosure headings are plain case, so units read "10s", never "10S". Shared
components cover buttons (`.btn` with primary/secondary/ghost/danger variants
and `.btn-sm`), bracketed panel controls (`.bracket-btn`), toggle groups
(`.segmented`), venue toggles and checkbox choices (`.chip`), legends, KPI
cells (`.kpi`), tables (`.data-table` with right-aligned `.num` columns and
`data-best` winner tint), grid-list header rows (`.list-head`), venue cells
shared by Execution and Margin (`.venue-heading`, `.venue-meta`), notices,
empty states, skeletons, scroll boxes that fade when more rows remain
(`.scroll-fade` with `Overseer.scrollFade`), disclosures, tooltips and
popovers. Mono carries data, labels, controls and system messages; sans is
reserved for titles and prose, and explanations live in each page's **How this
works** rather than the main view. Page JavaScript lives in `static/js/`, one
file per page, with any server values passed in a JSON config script.

Every displayed number, amount, time and age goes through `static/js/format.js`
(the global `Fmt`), so one value never prints two ways: signs use a true minus,
large amounts keep three significant figures with an upper-case suffix
(`$35.7B`, `$7.01B`, `$184M`, `$410K`), basis points are always `bps`, times
are always UTC (`15:19:00 UTC`, `26 Oct 15:19:00 UTC`; the server's `utc_time`
filter matches), ages read `12s`, `1m 05s`, `14m`, `2h`, `3d`, and missing
values print `—`. Words follow one vocabulary: **Asset** for the asset picker,
**Window** for the time range, durations as `8h`, `24h`, `7d`, `30d`,
"Unknown" for a value that exists but isn't known and "Unavailable" for one
that couldn't be fetched. Buy/sell, bids/asks and long/short use the blue and
purple direction colours; green and red are kept for money results.

Amber marks the brand, focus, the current location, the page's main view
switch (`.segmented-view`, at most one per page), the single primary action and
warnings. Filter toggles are quiet: the chosen option is raised like a selected
chip. Amber is never a data series colour. Colours come only from the Tailwind
tokens; venue identity colours come from `venues.js`, whose `chartOptions()`
gives every chart the same token-driven chrome and grouped, magnitude-aware
axis numbers. Every line chart shares one hover readout (`legendReadout`): the
legend lists each series with its latest value at one shared precision, and
while the pointer (or a long press) is on the plot the values follow the
crosshair, with the hovered time at the legend's end, instead of a floating
tooltip, stacked axis badges or dotted price lines. Numbers keep one precision
per column. Controls
are 36px tall with a mouse; touch devices (`pointer: coarse`) get 44px targets
and 16px inputs at any width. Signed money values use gain/loss tones, with zero
neutral. Visible text avoids characters the bundled Plex subsets lack (Δ, Σ, ≥,
≈, →), which would otherwise render in a fallback font.

On the market pages the status bar is the market feed: it leads with the age
of the newest ingested bar, its UTC timestamp, and an explicit Fresh / Delayed /
Stale state. It measures pipeline freshness across all markets, not every
displayed chart or funding rate. Age advances between checks; existing
thresholds remain 2 minutes for fresh and 5 minutes before stale.
Screen-refresh activity is labelled separately and never replaces the data age.
Failed checks and offline states retain the last-confirmed bar and its
advancing age; absent data or an unsuccessful first check shows no invented age
or timestamp. The timer is not a live announcement. A delayed feed tints the
status bar amber and a stale feed or failed check tints it red, rather than
adding a banner that would push the page down; screen readers hear the same
warning through a hidden live region. Positions, Execution estimates and the
Margin calculator fill the same bar with their own data age instead of the
unrelated historical feed, with **[refresh]** beside the age it resets (they
also refresh on their own when settings change or on a timer); the login page
has none. On phones the bar keeps its label, value, state and [refresh] on one
line by dropping secondary words such as "8/8 captured" (`.status-bar-wide`),
which screen readers still hear; warnings and errors always show.

Navigation keeps Charts, Basis, and Funding as primary pages. Tools opens a
disclosure containing Trade flow, Tracked wallets, Positions, Execution
estimates, Margin calculator and Depth heatmap; each retains its direct URL and
independent controls. Tools is highlighted on these pages. The full navigation
appears from 1024px wide and stays centred whether or not you are signed in;
narrower screens use **Menu**.
The disclosure supports normal Tab navigation, arrow-key entry, Escape, and
outside/focus-leave dismissal; on mobile, Escape closes Tools before closing the
main menu. The header, status bar, page content and footer share one aligned
column.

Page scope is explicit: Flow thresholds filter only its detail table, which
shows at most the latest 400 matching venue-minute rows; Wallets share meters
cover 24h across all assets and summaries cover all recorded fills. Funding
history labels raw settlement values separately from smoothed chart lines.
Flow lists only assets that a trade-streaming venue (`stream` in `symbols.toml`)
lists, so indices like NDX and SP500 aren't offered. It opens on BTC unless a
different asset was saved; an asset or window with
no trades shows "—" totals and says what to change. Every chart fits the whole
selected window, including on phones, and axis numbers are grouped (107,000).

Charts offers Price, Spread, and Volatility metrics in both Overview and the
multi-chart view. Volatility plots rolling realised volatility by venue; its
saved 1h/6h/24h calculation window defaults to 24h. The Window selector
(24h/48h/7d) controls the history displayed, independently of the rolling window.
`/api/realised-volatility` reads completed 1-minute candles plus the warm-up
history and computes `100 * sqrt(sum(log(close / previous_close)^2))`, without
annualisation. Plot points are sampled every 1/5/15 minutes for 24h/48h/7d views,
but every calculation still uses the underlying 1-minute returns.

The page's **How this works** disclosure explains the candles-or-lines view
and holds the volatility formula and missing-data explanation. On narrow
screens, all metric labels fit their selector while retaining 44px touch
targets.

Below 640px, chart filters, rolling-window controls and venue chips sit in the
shared **Filters** fold, collapsed initially. Its visible summary shows
the active market, asset or asset count, metric/window, window and selected
venue count. Changing selections leaves it open; saved selections still
survive reloads. Desktop keeps the original controls fully visible. Crossing
the breakpoint preserves settings and keeps focused controls reachable;
closing the mobile disclosure dismisses its pickers. The chart is visible
without first scrolling past the full filter panel.

The coloured exchange buttons below the filters toggle each venue immediately
across all Price, Spread, and Volatility charts. Unselected buttons remain visible
and clickable, including when no venues are selected. Selected buttons have a
raised fill and stronger border; unselected buttons dim their colour swatch, and
venue colours never change. **[all]** and **[none]** beside the Venues label
select or clear every venue at once, in the same global state. There are no
per-chart exchange or custom/reset controls.
The global choice persists across reloads and applies to newly added assets.
Missing venues stay selected globally, while each chart reports how many selected
venues have data. Saved per-chart overrides no longer affect rendering.

Hover the plot (long-press on touch screens) and each legend value follows the
crosshair, with the hovered time at the legend's end; off the plot the legend
shows the latest values. Price readouts use candle closes, spread readouts are
in bps, and volatility readouts include window coverage. Screen readers retain
latest values and unavailable venue names through each chart's availability note.

Only full rolling windows are plotted. Missing or invalid closes leave gaps;
separate line segments prevent interpolation across them. Hover readouts
show coverage and display partial windows as unavailable, not zero; availability
notes also flag incomplete latest windows. Valid flat-price windows show zero
volatility.

Market charts resize with the selected asset count: one chart is full-width
and tallest, two and three step down in height, and four retain the original
two-column desktop layout with 25rem cards (22rem on mobile). Smaller screens
keep charts stacked. Heights use viewport-aware bounds, and chart canvases
automatically resize when assets are added/removed or panels are resized.

Each panel has one size control: **[+]** makes it bigger (collapsed, then
normal, then enlarged over the page) and **[−]** smaller; at either end the
button that can't go further is dimmed. Escape returns an enlarged panel to
normal, and collapsed panels are remembered. A panel or page that fails to
load offers **[retry]** in the same bracketed style.

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
