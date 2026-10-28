/* Funding rates page: markets table/stack, market pulse and history chart. */
(() => {
'use strict';

// ---- venue links --------------------------------------------------------------
// Trade deep-links open the market directly. Referral attribution on most
// venues happens at SIGNUP, not per trade click — so paste your codes below
// and the venue column headers become your join/invite links (the income
// path), while cells stay direct trade links. Empty code = plain header.
const REFS = { binance: '', bybit: '', hyperliquid: '', lighter: '', extended: '', bulk: '' };

const TRADE_URLS = {
  binance:     s => `https://www.binance.com/en/futures/${s.replace('/','')}`,
  bybit:       s => `https://www.bybit.com/trade/usdt/${s.replace('/','')}`,
  hyperliquid: s => `https://app.hyperliquid.xyz/trade/${s}`,
  lighter:     s => `https://app.lighter.xyz/trade/${s}`,
  extended:    s => `https://app.extended.exchange/trade/${s}`,
  rise:        s => `https://www.rise.trade/en/trade/${s.split('/')[0]}`,  // base asset only, not the full pair
  bullet:      s => `https://app.bullet.xyz/trade/${s}`,
  bulk:        s => `https://app.bulk.trade/trade/${s}`,
};
const JOIN_URLS = {
  binance:     c => `https://accounts.binance.com/register?ref=${c}`,
  bybit:       c => `https://www.bybit.com/invite?ref=${c}`,
  hyperliquid: c => `https://app.hyperliquid.xyz/join/${c}`,
  lighter:     c => `https://app.lighter.xyz/join/${c}`,
  extended:    c => `https://app.extended.exchange/join/${c}`,
  bulk:        c => `https://app.bulk.trade/join/${c}`,
};
const tradeUrl = (ex, sym) => (TRADE_URLS[ex] || (() => null))(sym);

const tip = document.getElementById('tip');

function formatRateAge(timestamp) {
  const observed = Date.parse(timestamp);
  if (!Number.isFinite(observed)) return 'unknown';
  const minutes = Math.max(0, Math.floor((Date.now() - observed) / 60000));
  if (minutes < 1) return 'now';
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h`;
  return `${Math.floor(minutes / 1440)}d`;
}

function rateFreshness(v) {
  const observed = Date.parse(v.rate_ts);
  if (!Number.isFinite(observed)) {
    return { label: 'time unknown', visibleLabel: 'time unknown', stale: true };
  }
  const ageMinutes = Math.max(0, (Date.now() - observed) / 60000);
  const freshFor = v.rate_kind === 'current'
    ? 15
    : Math.max(75, Number(v.interval_hours || 1) * 60 + 15);
  const stale = ageMinutes > freshFor;
  const status = stale ? 'stale' : v.rate_kind === 'current' ? 'live' : 'settled';
  const label = `${status} · ${formatRateAge(v.rate_ts)}`;
  return {
    label,
    visibleLabel: stale || v.rate_kind !== 'current' ? label : '',
    stale,
  };
}

// Compact cells drop one decimal; the hover tip keeps full precision.
function displayedRate(v, compact = false) {
  if (rateMode === 'period') {
    const value = v.rate * 100;
    const digits = (Math.abs(value) < 0.1 ? 4 : 3) - (compact ? 1 : 0);
    return { value: Fmt.signedPercent(value, digits), interval: `${v.interval_hours}h` };
  }
  const digits = compact ? (Math.abs(v.apr_pct) >= 1000 ? 0 : 1) : 2;
  return { value: Fmt.signedPercent(v.apr_pct, digits), interval: '' };
}

// Shading always follows APR, whichever rate representation is displayed,
// so intensity stays comparable across venues.
function rateTint(v, rank) {
  const heat = Math.min(Math.abs(v.apr_pct) / 40, 1).toFixed(2);
  const sign = v.apr_pct > 0 ? 'pos' : v.apr_pct < 0 ? 'neg' : 'flat';
  return `data-sign="${sign}"${rank ? ` data-rank="${rank}"` : ''} style="--heat:${heat}"`;
}

function rateLink(v, { compact = false, venue = false } = {}) {
  const cls = v.apr_pct > 0 ? 'text-gain' : v.apr_pct < 0 ? 'text-loss' : 'text-dim';
  const href = tradeUrl(v.exchange, v.symbol);
  const shown = displayedRate(v, compact);
  const freshness = rateFreshness(v);
  const metadata = [shown.interval, freshness.visibleLabel].filter(Boolean).join(' · ');
  return `<a ${href ? `href="${href}" target="_blank" rel="noopener"` : ''}
       class="rate-link ${cls}"
       data-tip='${JSON.stringify({
            symbol: v.symbol, exchange: v.exchange,
            venueLabel: v.venue_label,
            rate: v.rate, apr: v.apr_pct, hours: v.interval_hours,
            kind: v.rate_kind, rateTs: v.rate_ts,
            settledRate: v.settled_rate, settledHours: v.settled_interval_hours,
            settledTs: v.funding_ts, nextFunding: v.next_funding_at,
            premium: v.funding_premium, index: v.index_price,
            oi: v.oi_notional, oiDelta: v.oi_delta_pct,
            vol: v.volume_24h, mark: v.mark_price })}'>
      ${venue ? `<span class="rate-venue" title="${v.venue_label}">${v.venue_label}</span>` : ''}
      <span class="rate-value">${shown.value}</span>
      ${metadata ? `<span class="rate-meta ${freshness.stale ? 'text-phosphor' : 'text-faint'}">${metadata}</span>` : ''}
    </a>`;
}

function cell(v, rank, compact) {
  if (!v) return '<td class="num text-faint">—</td>';
  return `<td class="num rate-cell" ${rateTint(v, rank)}>${rateLink(v, { compact })}</td>`;
}

const STAR_PATH = 'm12 17.75l-6.172 3.245l1.179-6.873l-5-4.867l6.9-1l3.086-6.253l3.086 6.253l6.9 1l-5 4.867l1.179 6.873z';
const starIcon = filled => `<svg class="h-4 w-4" viewBox="0 0 24 24" aria-hidden="true"><path ${filled
  ? 'fill="currentColor"'
  : 'fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"'
} d="${STAR_PATH}"/></svg>`;

// Ticker only, so nothing truncates; the full name is the title and is read
// out to screen readers.
function coinName(coin, rows) {
  const label = rows[0]?.asset_label;
  const named = label && label !== coin;
  return `<span class="font-semibold text-body"${named ? ` title="${label}"` : ''}>${coin}</span>${named
    ? `<span class="sr-only"> ${label}</span>` : ''}`;
}

function watchButton(coin, rows) {
  const label = rows[0]?.asset_label;
  const watched = watchedAssets.has(coin);
  const action = `${watched ? 'Remove' : 'Add'} ${label || coin} ${watched ? 'from' : 'to'} watchlist`;
  return `<button type="button" class="watch-btn" data-watch="${coin}" aria-pressed="${watched}"
    aria-label="${action}" title="${action}">${starIcon(watched)}</button>`;
}

const venueRows = rows => Object.fromEntries(rows.map(row => [row.venue_key || row.exchange, row]));

// Marks the strongest and weakest venue for an asset, when they differ.
function rankingFor(rows) {
  const aprs = rows.map(row => row.apr_pct);
  const best = Math.max(...aprs);
  const worst = Math.min(...aprs);
  return row => {
    if (!row || rows.length < 2 || best === worst) return null;
    return row.apr_pct === best ? 'best' : row.apr_pct === worst ? 'worst' : null;
  };
}

function marketTableHead(venues) {
  return '<th scope="col" class="coin-col">Asset</th>' + venues.map(venue => {
    const code = REFS[venue.exchange];
    const joinUrl = JOIN_URLS[venue.exchange];
    // Long names may break after "hip3:", never inside a word.
    const label = venue.label.replace(':', ':<wbr>');
    return `<th scope="col" class="num venue-col">${
      code && joinUrl
        ? `<a href="${joinUrl(code)}" target="_blank" rel="noopener"
              class="underline decoration-faint decoration-dotted hover:text-body">${label}</a>`
        : label}</th>`;
  }).join('');
}

function marketTableBody({ coins, venues }, compact) {
  return coins.map(([coin, rows]) => {
    const byVenue = venueRows(rows);
    const rank = rankingFor(rows);
    return `<tr>
      <td class="coin-col"><div class="coin-cell">
        <div class="min-w-0 flex-1">${coinName(coin, rows)}</div>${watchButton(coin, rows)}
      </div></td>
      ${venues.map(venue => cell(byVenue[venue.key], rank(byVenue[venue.key]), compact)).join('')}
    </tr>`;
  }).join('');
}

// Narrow layout: one block per asset, its venues in a wrapping grid.
function marketStack({ coins, venues }) {
  return coins.map(([coin, rows]) => {
    const byVenue = venueRows(rows);
    const rank = rankingFor(rows);
    const aprs = rows.map(row => row.apr_pct);
    const spread = rows.length > 1 ? Math.max(...aprs) - Math.min(...aprs) : null;
    return `<article class="funding-stack-coin" role="listitem">
      <div class="funding-stack-head">
        <div class="min-w-0 flex-1">${coinName(coin, rows)}</div>
        ${spread === null ? '' : `<span class="funding-stack-spread">Spread ${Fmt.percent(spread, 2)}</span>`}
        ${watchButton(coin, rows)}
      </div>
      <div class="funding-stack-rates">${venues.map(venue => byVenue[venue.key]).filter(Boolean)
        .map(row => `<div class="rate-cell" ${rateTint(row, rank(row))}>${rateLink(row, { venue: true })}</div>`)
        .join('')}</div>
    </article>`;
  }).join('');
}

const storedFundingFilters = Overseer.filterState('funding');
const fundingUrl = new URLSearchParams(window.location.search);
const fundingFilterKeys = new Set([
  'search', 'view', 'family', 'assetClass', 'asset', 'hhours', 'hbucket',
  'smoothing', 'venues', 'rate',
]);
const hasFundingUrlState = [...fundingUrl.keys()].some(key => fundingFilterKeys.has(key));
const fundingFilters = Object.freeze({
  get(name) {
    return hasFundingUrlState ? fundingUrl.get(name) : storedFundingFilters.get(name);
  },
  set(name, value) {
    storedFundingFilters.set(name, value);
    if (value == null || value === '') fundingUrl.delete(name);
    else fundingUrl.set(name, String(value));
    const query = fundingUrl.toString();
    window.history.replaceState(null, '',
      `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`);
  },
  getList(name) {
    try {
      const value = JSON.parse(this.get(name) || '[]');
      return Array.isArray(value) ? value.filter(item => typeof item === 'string') : [];
    } catch (error) {
      return [];
    }
  },
  setList(name, values) {
    this.set(name, JSON.stringify([...values]));
  },
  restoreSelect(select, name = select.id) {
    const saved = this.get(name);
    if (saved === null || ![...select.options].some(option => option.value === saved)) return false;
    select.value = saved;
    return true;
  },
});
document.getElementById('ffilter').value = fundingFilters.get('search') || '';
let activeView = fundingFilters.get('view') === 'history' ? 'history' : 'table';
let activeFamily = ['crypto', 'traditional'].includes(fundingFilters.get('family'))
  ? fundingFilters.get('family') : 'all';
let activeAssetClass = ['commodity', 'index', 'equity'].includes(fundingFilters.get('assetClass'))
  ? fundingFilters.get('assetClass') : 'all';
let watchedAssets = new Set(storedFundingFilters.getList('watchlist'));
let watchlistOnly = storedFundingFilters.get('watchlistOnly') === 'true';
let rateMode = fundingFilters.get('rate') === 'period' ? 'period' : 'apr';
let lastFundingData = null;   // reused by the history tab's asset selector
const fundingState = Overseer.pageState();
let historyRequest = 0;
let historyLoadKey = null;

function fundingAssetForAlias(data, candidate) {
  if (Object.prototype.hasOwnProperty.call(data, candidate)) return candidate;
  return Object.entries(data).find(([, rows]) =>
    rows.some(row => row.symbol === candidate))?.[0] || candidate;
}

function reconcileWatchedAssetAliases(data) {
  const normalised = new Set(
    [...watchedAssets].map(asset => fundingAssetForAlias(data, asset)));
  const changed = normalised.size !== watchedAssets.size
    || [...normalised].some(asset => !watchedAssets.has(asset));
  if (!changed) return;
  watchedAssets = normalised;
  storedFundingFilters.setList('watchlist', watchedAssets);
}

const assetScopeControl = document.getElementById('asset-scope-control');
const assetScopeTrigger = document.getElementById('asset-scope-trigger');
const assetScopeMenu = document.getElementById('asset-scope-menu');
const assetScopeValue = document.getElementById('asset-scope-value');
const assetScopeOptions = [...assetScopeMenu.querySelectorAll('[data-scope-option]')];


function currentAssetScope() {
  if (activeFamily === 'crypto') return 'crypto';
  if (activeFamily !== 'traditional') return 'all';
  return activeAssetClass === 'all' ? 'traditional' : activeAssetClass;
}

function syncAssetScopeMenu() {
  const selectedValue = currentAssetScope();
  assetScopeOptions.forEach(option => {
    option.setAttribute('aria-checked', String(option.dataset.scopeOption === selectedValue));
  });
  const selected = assetScopeOptions.find(option => option.dataset.scopeOption === selectedValue);
  if (!selected) return;
  const label = selectedValue === 'traditional'
    ? 'Traditional'
    : selected.querySelector('span')?.textContent || selectedValue;
  const count = selected.querySelector('[data-scope-count]')?.textContent || '0';
  assetScopeValue.textContent = `${label} · ${count}`;
}

function closeAssetScopeMenu(restoreFocus = false) {
  assetScopeMenu.classList.add('hidden');
  assetScopeTrigger.setAttribute('aria-expanded', 'false');
  if (restoreFocus) assetScopeTrigger.focus();
}

function openAssetScopeMenu() {
  assetScopeMenu.classList.remove('hidden');
  assetScopeTrigger.setAttribute('aria-expanded', 'true');
  const selectedIndex = Math.max(
    0,
    assetScopeOptions.findIndex(option => option.dataset.scopeOption === currentAssetScope()),
  );
  requestAnimationFrame(() => assetScopeOptions[selectedIndex].focus());
}

function chooseAssetScope(scope) {
  if (['commodity', 'index', 'equity'].includes(scope)) {
    activeFamily = 'traditional';
    activeAssetClass = scope;
  } else {
    activeFamily = scope === 'crypto' || scope === 'traditional' ? scope : 'all';
    activeAssetClass = 'all';
  }
  fundingFilters.set('family', activeFamily === 'all' ? null : activeFamily);
  fundingFilters.set('assetClass', activeAssetClass === 'all' ? null : activeAssetClass);
  lastHistoryData = null;
  lastOiData = null;
  syncAssetScopeMenu();
  closeAssetScopeMenu(true);
  load();
}

assetScopeTrigger.addEventListener('click', () => {
  if (assetScopeTrigger.getAttribute('aria-expanded') === 'true') {
    closeAssetScopeMenu();
  } else {
    openAssetScopeMenu();
  }
});
assetScopeTrigger.addEventListener('keydown', event => {
  if (event.key === 'Escape' && assetScopeTrigger.getAttribute('aria-expanded') === 'true') {
    event.preventDefault();
    closeAssetScopeMenu(true);
    return;
  }
  if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
  event.preventDefault();
  openAssetScopeMenu();
});
assetScopeMenu.addEventListener('click', event => {
  const option = event.target.closest('[data-scope-option]');
  if (option) chooseAssetScope(option.dataset.scopeOption);
});
assetScopeMenu.addEventListener('keydown', event => {
  const option = event.target.closest('[data-scope-option]');
  if (!option) return;
  const index = assetScopeOptions.indexOf(option);
  let nextIndex = null;
  if (event.key === 'ArrowDown') nextIndex = (index + 1) % assetScopeOptions.length;
  if (event.key === 'ArrowUp') nextIndex = (index - 1 + assetScopeOptions.length) % assetScopeOptions.length;
  if (event.key === 'Home') nextIndex = 0;
  if (event.key === 'End') nextIndex = assetScopeOptions.length - 1;
  if (nextIndex !== null) {
    event.preventDefault();
    assetScopeOptions[nextIndex].focus();
  } else if (event.key === 'Escape') {
    event.preventDefault();
    closeAssetScopeMenu(true);
  }
});
assetScopeControl.addEventListener('focusout', () => {
  requestAnimationFrame(() => {
    if (!assetScopeControl.contains(document.activeElement)) closeAssetScopeMenu();
  });
});
document.addEventListener('pointerdown', event => {
  if (!assetScopeControl.contains(event.target)) closeAssetScopeMenu();
});

function fundingEntries(data, includeSearch = true) {
  const q = includeSearch
    ? (document.getElementById('ffilter').value || '').trim().toUpperCase()
    : '';
  return Object.entries(data).filter(([asset, rows]) => {
    const row = rows[0] || {};
    if (watchlistOnly && !watchedAssets.has(asset)) return false;
    if (activeFamily !== 'all' && row.asset_family !== activeFamily) return false;
    if (activeFamily === 'traditional' && activeAssetClass !== 'all'
        && row.asset_class !== activeAssetClass) return false;
    return !q || asset.toUpperCase().includes(q)
      || (row.asset_label || '').toUpperCase().includes(q);
  });
}

function renderAssetFilters(data) {
  const entries = Object.entries(data);
  const count = predicate => entries.filter(([, rows]) => predicate(rows[0] || {})).length;
  const counts = {
    crypto: count(row => row.asset_family === 'crypto'),
    traditional: count(row => row.asset_family === 'traditional'),
    all: entries.length,
    commodity: count(row => row.asset_class === 'commodity'),
    index: count(row => row.asset_class === 'index'),
    equity: count(row => row.asset_class === 'equity'),
    watchlist: entries.filter(([asset]) => watchedAssets.has(asset)).length,
  };
  document.getElementById('watchlist-count').textContent = counts.watchlist;
  Object.entries(counts).forEach(([name, value]) => {
    const countElement = assetScopeMenu.querySelector(`[data-scope-count="${name}"]`);
    if (countElement) countElement.textContent = value;
  });
  syncAssetScopeMenu();
  document.getElementById('watchlist-filter').setAttribute('aria-pressed', String(watchlistOnly));
}

// ---- markets: never scrolls sideways; height fitted to the screen -----------
const marketsCard = document.getElementById('fcard');
const marketsScroll = document.getElementById('fmarkets');
const marketsTable = document.getElementById('ftable');
const marketsStack = document.getElementById('fstack');
const marketsEmpty = document.getElementById('fempty');
let marketsModel = null;   // { coins, venues, hasFunding } from the latest load
let marketsWidth = 0;      // scroller width the current layout was chosen for

function marketsEmptyCopy() {
  if (!marketsModel.hasFunding) return 'No funding rates yet. Settlements usually arrive within the hour.';
  return watchlistOnly && watchedAssets.size === 0
    ? 'Your watchlist is empty. Use the star beside an asset to add it.'
    : 'No assets match these filters. Clear the search or choose another asset group.';
}

// Tries the full table, then a compact one; when even that is wider than the
// card, switches to the stacked list. Measured rather than estimated, so it
// holds for any number of venues and any label length.
function renderMarkets() {
  if (!marketsModel) return;
  marketsWidth = marketsScroll.clientWidth;
  const { coins, venues } = marketsModel;
  marketsEmpty.hidden = coins.length > 0;
  if (!coins.length) {
    marketsTable.hidden = true;
    marketsStack.hidden = true;
    marketsEmpty.textContent = marketsEmptyCopy();
    fitMarketsHeight();
    return;
  }
  const body = document.getElementById('fbody');
  document.getElementById('fhead').innerHTML = marketTableHead(venues);
  marketsTable.hidden = false;
  marketsStack.hidden = true;
  let fits = false;
  for (const density of ['comfortable', 'compact']) {
    marketsTable.dataset.density = density;
    body.innerHTML = marketTableBody(marketsModel, density === 'compact');
    // A hidden tab measures zero; the layout is chosen again once it is shown.
    if (!marketsWidth || marketsTable.offsetWidth <= marketsScroll.clientWidth) {
      fits = true;
      break;
    }
  }
  if (fits) {
    marketsStack.replaceChildren();
  } else {
    marketsTable.hidden = true;
    body.replaceChildren();
    marketsStack.innerHTML = marketStack(marketsModel);
    marketsStack.hidden = false;
  }
  fitMarketsHeight();
}

const MARKETS_GAP = 16;            // px kept between the card and the fold
const MARKETS_MIN_ROWS = 8;        // table rows the card always has room for
const MARKETS_STACK_HEIGHT = 640;  // stacked blocks are tall; show several at once

// Room for the card's header, the table head and MARKETS_MIN_ROWS rows,
// measured so touch-sized rows get the same row count.
function marketsMinHeight() {
  const row = marketsTable.hidden ? null : marketsTable.querySelector('tbody tr');
  if (!row) return MARKETS_STACK_HEIGHT;
  const chrome = marketsCard.offsetHeight - marketsScroll.clientHeight;
  return chrome + marketsTable.tHead.offsetHeight + MARKETS_MIN_ROWS * row.offsetHeight;
}

// The card ends just above the fold when that leaves room for at least
// MARKETS_MIN_ROWS rows, so the page need not scroll before the rows do; on
// shorter screens it keeps that minimum and runs past the fold. Never taller
// than the screen. Document-relative, so it ignores the scroll position.
function fitMarketsHeight() {
  if (!marketsCard.offsetParent) return;
  const top = marketsCard.getBoundingClientRect().top + window.scrollY;
  const remaining = window.innerHeight - top - MARKETS_GAP;
  const screen = window.innerHeight - 2 * MARKETS_GAP;
  const height = Math.max(remaining, Math.min(screen, marketsMinHeight()));
  marketsCard.style.setProperty('--markets-max', `${Math.floor(height)}px`);
  updateMarketsFade();
}

// Fades the last visible row while more rows remain below.
const updateMarketsFade = Overseer.scrollFade(marketsScroll);

let marketsFrame = 0;
let marketsRelayout = false;
function scheduleMarkets(relayout = false) {
  marketsRelayout ||= relayout;
  if (marketsFrame) return;
  marketsFrame = requestAnimationFrame(() => {
    marketsFrame = 0;
    const relayoutNow = marketsRelayout;
    marketsRelayout = false;
    if (relayoutNow) renderMarkets();
    else fitMarketsHeight();
  });
}
// Width decides the layout; anything that moves the card refits its height.
new ResizeObserver(() => scheduleMarkets(marketsScroll.clientWidth !== marketsWidth)).observe(marketsScroll);
new ResizeObserver(() => scheduleMarkets()).observe(document.body);
window.addEventListener('resize', () => scheduleMarkets());
document.fonts?.ready.then(() => scheduleMarkets(true));

async function load({ forceRefresh = false } = {}) {
  const dot = document.getElementById('refresh-dot');
  fundingState.loading('Loading current funding rates');
  try {
    const data = await Overseer.requestJSON('/api/funding', { forceRefresh });
    reconcileWatchedAssetAliases(data);
    lastFundingData = data;
    populateCoinSelector(data);
    if (activeView === 'history' && (lastHistoryData === null || forceRefresh)) {
      await loadHistory({ forceRefresh });
    }
    // dislocation-first: sort by max-min APR spread so memecoin/new-listing
    // outliers surface at the top of a wide-universe table
    const spread = rows => {
      const a = rows.map(r => r.apr_pct);
      return a.length > 1 ? Math.max(...a) - Math.min(...a) : Math.abs(a[0] ?? 0);
    };
    renderAssetFilters(data);
    const coins = fundingEntries(data)
      .sort(([, ra], [, rb]) => spread(rb) - spread(ra));
    document.getElementById('market-count').textContent = coins.length;
    const venuesByKey = new Map();
    coins.forEach(([, rows]) => rows.forEach(row => {
      const key = row.venue_key || row.exchange;
      if (!venuesByKey.has(key)) venuesByKey.set(key, {
        key,
        label: row.venue_label || row.exchange,
        exchange: row.exchange,
      });
    }));
    const venues = [...venuesByKey.values()].sort((a, b) => {
      const ai = VENUE_ORDER.indexOf(a.key);
      const bi = VENUE_ORDER.indexOf(b.key);
      if (ai !== -1 || bi !== -1) return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
      return a.label.localeCompare(b.label);
    });
    marketsModel = { coins, venues, hasFunding: Object.keys(data).length > 0 };
    renderMarkets();
    if (!coins.length) {
      document.getElementById('highlights').innerHTML = '';
      document.getElementById('market-pulse').hidden = true;
      if (marketsModel.hasFunding) fundingState.ready();
      else fundingState.empty('No funding rates yet. Settlements usually arrive within the hour.');
      return;
    }
    // ---- market pulse: superlatives across the filtered universe -----------
    const flat = coins.flatMap(([coin, rows]) => rows.map(r => ({coin, ...r})));
    const spreads2 = coins
      .map(([coin, rows]) => {
        if (rows.length < 2) return null;
        const hi = rows.reduce((a,b) => a.apr_pct > b.apr_pct ? a : b);
        const lo = rows.reduce((a,b) => a.apr_pct < b.apr_pct ? a : b);
        return { coin, spread: hi.apr_pct - lo.apr_pct, hi, lo };
      }).filter(Boolean).sort((a,b) => b.spread - a.spread);
    const richest = flat.reduce((a,b) => (b.apr_pct > (a?.apr_pct ?? -1e9) ? b : a), null);
    const mostNeg = flat.reduce((a,b) => (b.apr_pct < (a?.apr_pct ?? 1e9) ? b : a), null);
    const mostLiq = flat.filter(r => r.oi_notional)
      .reduce((a,b) => (b.oi_notional > (a?.oi_notional ?? -1) ? b : a), null);
    // href2: a second venue tab opened by JS in the same click — browsers
    // allow one window.open() per gesture alongside the anchor navigation,
    // so both legs of a spread open without tripping the popup blocker.
    const card = (label, big, cls, sub, href, href2) => `
      <a ${href ? `href="${href}" target="_blank" rel="noopener"` : ''}
         ${href2 ? `data-href2="${href2}"` : ''}
         class="kpi block transition-colors hover:bg-panel2/40">
        <p class="kpi-label">${label}</p>
        <p class="kpi-value ${cls}">${big}</p>
        <p class="kpi-meta">${sub}</p></a>`;
    const signedTone = value => value > 0 ? 'text-gain' : value < 0 ? 'text-loss' : 'text-body';
    const hl = document.getElementById('highlights');
    document.getElementById('market-pulse').hidden = false;
    hl.innerHTML = [
      spreads2[0] && card('Largest APR spread', Fmt.percent(spreads2[0].spread, 2), 'text-body',
        `${spreads2[0].coin} · ${spreads2[0].hi.venue_label} vs ${spreads2[0].lo.venue_label} · opens both`,
        tradeUrl(spreads2[0].hi.exchange, spreads2[0].hi.symbol),
        tradeUrl(spreads2[0].lo.exchange, spreads2[0].lo.symbol)),
      richest && card('Highest APR', Fmt.signedPercent(richest.apr_pct, 2),
        signedTone(richest.apr_pct),
        `${richest.coin} · ${richest.venue_label}`, tradeUrl(richest.exchange, richest.symbol)),
      mostNeg && card('Lowest APR', Fmt.signedPercent(mostNeg.apr_pct, 2),
        signedTone(mostNeg.apr_pct),
        `${mostNeg.coin} · ${mostNeg.venue_label}`, tradeUrl(mostNeg.exchange, mostNeg.symbol)),
      mostLiq && card('Most liquid', Fmt.usdCompact(mostLiq.oi_notional), 'text-body',
        `${mostLiq.coin} · ${mostLiq.venue_label} open interest`,
        tradeUrl(mostLiq.exchange, mostLiq.symbol)),
    ].filter(Boolean).join('');

    fundingState.ready();
    const latestRateTimestamp = Math.max(
      ...fundingEntries(data).flatMap(([, rows]) => rows)
        .map(row => Date.parse(row.rate_ts))
        .filter(Number.isFinite),
    );
    const latestRateAt = Number.isFinite(latestRateTimestamp) ? Fmt.time(latestRateTimestamp) : null;
    const refreshLabel = document.getElementById('refresh-label');
    dot.classList.remove('text-faint', 'text-loss');
    dot.classList.add('text-gain');
    refreshLabel.textContent = `Data · ${latestRateAt || 'time unknown'}`;
    document.getElementById('refresh-status').title = latestRateAt
      ? `Newest funding observation: ${Fmt.dateTime(latestRateTimestamp)}`
      : 'The newest funding timestamp is unavailable';
  } catch (e) {
    dot.classList.remove('text-faint', 'text-gain');
    dot.classList.add('text-loss');
    document.getElementById('refresh-label').textContent = 'Refresh failed — retrying';
    fundingState.error('Funding rates', load);
  }
}

function fundingPaymentExample(rate, hours) {
  if (!Number.isFinite(rate) || !Number.isFinite(hours) || hours <= 0) return null;
  const hourly = Math.abs(rate) * 1000 / hours;
  return {
    hourly,
    annualised: hourly * 8760,
    direction: rate > 0 ? 'long pays · short earns'
      : rate < 0 ? 'long earns · short pays'
      : 'no payment',
  };
}
const fundingUsd = value => Fmt.usd(value, value > 0 && value < 0.01 ? 4 : 2);
function positionFundingTip(left, top) {
  const edge = 16;
  tip.style.left = Math.max(edge, Math.min(left, window.innerWidth - tip.offsetWidth - edge)) + 'px';
  tip.style.top = Math.max(edge, Math.min(top, window.innerHeight - tip.offsetHeight - edge)) + 'px';
}
function showFundingTip(el) {
  const d = JSON.parse(el.dataset.tip);
  const rateTone = d.rate > 0 ? 'text-gain' : d.rate < 0 ? 'text-loss' : 'text-dim';
  const payment = fundingPaymentExample(d.rate, d.hours);
  const freshness = rateFreshness({
    rate_ts: d.rateTs,
    rate_kind: d.kind,
    interval_hours: d.hours,
  });
  const price = fixedFormatter([d.index, d.mark]);
  const paymentHtml = payment ? `
    <div class="my-2 border-y border-line-soft py-2">
      <div class="mb-0.5 text-body">${Fmt.usd(1000, 0)} notional</div>
      <div class="mb-1.5 text-faint">${payment.direction}</div>
      <div class="grid grid-cols-2 gap-x-3 gap-y-1 text-dim">
        <span>Per hour</span>
        <span class="text-right tabular-nums text-body">${fundingUsd(payment.hourly)}</span>
        <span>Per year*</span>
        <span class="text-right tabular-nums text-body">${fundingUsd(payment.annualised)}</span>
      </div>
      <div class="mt-1 text-right text-label text-faint">* if the rate stayed unchanged</div>
    </div>` : '';
  tip.innerHTML = `
    <div class="mb-2 flex items-baseline justify-between">
      <span class="font-semibold text-body">${d.venueLabel || d.exchange}</span>
      <span class="text-dim">${d.symbol}</span></div>
    <div class="grid grid-cols-2 gap-x-3 gap-y-1 text-dim">
      <span class="${rateTone}">${d.kind === 'current' ? 'Current' : 'Latest settled'} / ${d.hours}h</span>
      <span class="text-right tabular-nums ${rateTone}">${Fmt.signedPercent(d.rate * 100, 4)}</span>
      <span>Annualised</span>
      <span class="text-right tabular-nums ${rateTone}">${Fmt.signedPercent(d.apr, 2)}</span>
    </div>
    ${paymentHtml}
    <div class="grid grid-cols-2 gap-x-3 gap-y-1 text-dim">
      <span>Index / oracle</span>
      <span class="text-right tabular-nums text-body">${price(d.index)}</span>
      <span>Mark</span>
      <span class="text-right tabular-nums text-body">${price(d.mark)}</span>
      <span>Open interest</span>
      <span class="text-right tabular-nums text-body">${Fmt.usdCompact(d.oi)}</span>
      <span>OI 24h change</span>
      <span class="text-right tabular-nums ${d.oiDelta == null ? 'text-dim' : d.oiDelta >= 0 ? 'text-gain' : 'text-loss'}">${
        Fmt.signedPercent(d.oiDelta, 1)}</span>
      <span>24h volume</span>
      <span class="text-right tabular-nums text-body">${Fmt.usdCompact(d.vol)}</span>
    </div>
    <div class="mt-2 border-t border-line-soft pt-1.5 text-label ${freshness.stale ? 'text-loss' : 'text-faint'}">
      ${freshness.label} · ${d.kind === 'current'
        ? 'updated ' + Fmt.dateTime(d.rateTs) + (d.nextFunding ? ' · next ' + Fmt.dateTime(d.nextFunding) : '')
        : 'settled ' + Fmt.dateTime(d.settledTs)}</div>`;
  el.setAttribute('aria-describedby', 'tip');
  tip.classList.remove('hidden');
}
function hideFundingTip(el) {
  el?.removeAttribute('aria-describedby');
  tip.classList.add('hidden');
}
document.addEventListener('mouseover', e => {
  const el = e.target.closest('[data-tip]');
  if (!el) { hideFundingTip(); return; }
  showFundingTip(el);
});
document.addEventListener('focusin', e => {
  const el = e.target.closest('[data-tip]');
  if (!el) return;
  showFundingTip(el);
  const rect = el.getBoundingClientRect();
  positionFundingTip(rect.left, rect.bottom + 8);
});
document.addEventListener('focusout', e => {
  const el = e.target.closest('[data-tip]');
  if (el) hideFundingTip(el);
});
document.addEventListener('mousemove', e => {
  if (tip.classList.contains('hidden')) return;
  positionFundingTip(e.clientX + 14, e.clientY + 14);
});

// spread card: open the second venue alongside the anchor's own navigation.
// Skip modified clicks (ctrl/cmd/shift/middle) — the user is choosing how to
// open the FIRST link; forcing a second tab on top of that is a surprise.
document.getElementById('highlights').addEventListener('click', e => {
  const el = e.target.closest('[data-href2]');
  if (!el || e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
  window.open(el.dataset.href2, '_blank', 'noopener');
});

const rateModeControls = [...document.querySelectorAll('[data-rate-menu]')];

function syncRateModeMenus() {
  const label = rateMode === 'period' ? 'Per period' : 'Annualised';
  rateModeControls.forEach(control => {
    control.querySelector('[data-rate-value]').textContent = label;
    control.querySelectorAll('[data-rate-option]').forEach(option => {
      option.setAttribute('aria-checked', String(option.dataset.rateOption === rateMode));
    });
  });
}

function closeRateModeMenu(control, restoreFocus = false) {
  control.querySelector('[role="menu"]').classList.add('hidden');
  const trigger = control.querySelector('[data-rate-trigger]');
  trigger.setAttribute('aria-expanded', 'false');
  if (restoreFocus) trigger.focus();
}

function closeRateModeMenus(except = null) {
  rateModeControls.forEach(control => {
    if (control !== except) closeRateModeMenu(control);
  });
}

function openRateModeMenu(control) {
  closeRateModeMenus(control);
  const trigger = control.querySelector('[data-rate-trigger]');
  const menu = control.querySelector('[role="menu"]');
  const options = [...control.querySelectorAll('[data-rate-option]')];
  menu.classList.remove('hidden');
  trigger.setAttribute('aria-expanded', 'true');
  const selectedIndex = Math.max(0, options.findIndex(option => option.dataset.rateOption === rateMode));
  requestAnimationFrame(() => options[selectedIndex].focus());
}

function setRateMode(mode, redraw = true) {
  rateMode = mode === 'period' ? 'period' : 'apr';
  fundingFilters.set('rate', rateMode === 'apr' ? null : rateMode);
  syncRateModeMenus();
  document.getElementById('table-help').textContent = rateMode === 'apr'
    ? 'Sorted by widest APR spread'
    : "Each venue's own interval · sorted by APR";
  updateHistoryHelp();
  if (redraw) {
    if (lastFundingData) load();
    if (lastHistoryData) renderHistoryChart();
  }
}

rateModeControls.forEach(control => {
  const trigger = control.querySelector('[data-rate-trigger]');
  const options = [...control.querySelectorAll('[data-rate-option]')];
  trigger.addEventListener('click', () => {
    if (trigger.getAttribute('aria-expanded') === 'true') {
      closeRateModeMenu(control);
    } else {
      openRateModeMenu(control);
    }
  });
  trigger.addEventListener('keydown', event => {
    if (event.key === 'Escape' && trigger.getAttribute('aria-expanded') === 'true') {
      event.preventDefault();
      closeRateModeMenu(control, true);
      return;
    }
    if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
    event.preventDefault();
    openRateModeMenu(control);
  });
  control.addEventListener('click', event => {
    const option = event.target.closest('[data-rate-option]');
    if (!option) return;
    closeRateModeMenu(control, true);
    setRateMode(option.dataset.rateOption);
  });
  control.addEventListener('keydown', event => {
    const option = event.target.closest('[data-rate-option]');
    if (!option) return;
    const index = options.indexOf(option);
    let nextIndex = null;
    if (event.key === 'ArrowDown') nextIndex = (index + 1) % options.length;
    if (event.key === 'ArrowUp') nextIndex = (index - 1 + options.length) % options.length;
    if (event.key === 'Home') nextIndex = 0;
    if (event.key === 'End') nextIndex = options.length - 1;
    if (nextIndex !== null) {
      event.preventDefault();
      options[nextIndex].focus();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closeRateModeMenu(control, true);
    }
  });
  control.addEventListener('focusout', () => {
    requestAnimationFrame(() => {
      if (!control.contains(document.activeElement)) closeRateModeMenu(control);
    });
  });
});
document.addEventListener('pointerdown', event => {
  if (!event.target.closest('[data-rate-menu]')) closeRateModeMenus();
});

document.getElementById('watchlist-filter').addEventListener('click', () => {
  watchlistOnly = !watchlistOnly;
  storedFundingFilters.set('watchlistOnly', watchlistOnly ? 'true' : null);
  lastHistoryData = null;
  lastOiData = null;
  load();
});
marketsScroll.addEventListener('click', event => {
  const button = event.target.closest('[data-watch]');
  if (!button) return;
  const asset = button.dataset.watch;
  if (watchedAssets.has(asset)) watchedAssets.delete(asset);
  else watchedAssets.add(asset);
  storedFundingFilters.setList('watchlist', watchedAssets);
  lastHistoryData = null;
  lastOiData = null;
  load();
});

document.getElementById('ffilter').addEventListener('input', event => {
  fundingFilters.set('search', event.target.value);
  load();
});
Overseer.poll(load, 300);

// ---- historic view: one funding-APR line per venue, per coin ----------------

function populateCoinSelector(data) {
  const sel = document.getElementById('hcoin');
  const prev = sel.value;
  const saved = fundingAssetForAlias(data, fundingFilters.get('asset'));
  const assets = fundingEntries(data).sort(([a], [b]) => a.localeCompare(b));
  sel.innerHTML = assets.map(([asset, rows]) => {
    const label = rows[0]?.asset_label;
    const text = label && label !== asset ? `${asset} · ${label}` : asset;
    return `<option value="${asset}">${text}</option>`;
  }).join('');
  const names = assets.map(([asset]) => asset);
  if (names.includes(prev)) sel.value = prev;
  else if (names.includes(saved)) sel.value = saved;
  fundingFilters.set('asset', sel.value || null);
}

let hChart = null;
let hReadout = null;      // legend readout: one entry per venue, then the OI total
let hHandles = [];        // funding-rate line series
let hOiHandles = [];      // OI-notional bar series (bottom pane)
let hiddenVenues = new Set(fundingFilters.getList('venues'));
let smoothMode = fundingFilters.get('smoothing') === 'raw' ? 'raw' : 'ewma';
let lastHistoryData = null;      // cached fetches, so toggling/smoothing re-renders without a round trip
let lastOiData = null;

function ensureHistoryChart() {
  if (hChart) return hChart;
  hChart = LightweightCharts.createChart(document.getElementById('hchart'), chartOptions({
    // Only the vertical guide stays: it anchors the legend readout, which
    // lists every venue at that moment; a horizontal guide adds noise.
    crosshair: {
      vertLine: { labelVisible: true },
      horzLine: { visible: false, labelVisible: true },
    },
    // reserve the bottom ~30% for the OI pane so funding lines don't run
    // through it — same layout idea as volume-under-price on a candle chart.
    rightPriceScale: { scaleMargins: { top: 0.05, bottom: 0.3 } },
  }));
  hReadout = legendReadout(hChart, document.getElementById('hlegend'));
  return hChart;
}

// Venues settle at their own times, so the readout shows each venue's latest
// value at or before the hovered moment rather than only exact-time matches.
function valueAtOrBefore(points, time) {
  let lo = 0;
  let hi = points.length - 1;
  let found = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid].time <= time) { found = points[mid]; lo = mid + 1; } else hi = mid - 1;
  }
  return found?.value;
}

// EWMA follows the selected representation over each venue's OWN settlement
// sequence; it never smooths across venues with different settlement cadences.
const historyRate = point => rateMode === 'period' ? point.rate_pct : point.apr_pct;
function historyRateText(value, intervalHours = null) {
  const digits = rateMode === 'period' ? 4 : 1;
  const interval = rateMode === 'period' && intervalHours ? ` / ${intervalHours}h` : '';
  return `${Fmt.signedPercent(value, digits)}${interval}`;
}
function ewma(points, alpha = 0.35) {
  let s = null;
  return points.map(point => {
    const value = historyRate(point);
    s = s === null ? value : alpha * value + (1 - alpha) * s;
    return { time: point.time, value: s };
  });
}

// suggested bar width per window — the user can still override via #hbucket
// directly; this only fires when #hhours itself changes, so a manual choice
// sticks until they pick a different window.
const DEFAULT_BUCKET = { 24: 5, 48: 5, 168: 60, 720: 240 };

async function loadHistory({ forceRefresh = true } = {}) {
  const asset = document.getElementById('hcoin').value;
  const hours = document.getElementById('hhours').value;
  const bucket = document.getElementById('hbucket').value;
  if (!asset) return;
  const loadKey = `${asset}|${hours}|${bucket}`;
  if (historyLoadKey === loadKey) return;
  historyLoadKey = loadKey;
  const request = ++historyRequest;
  const qs = new URLSearchParams({ asset, hours, bucket });
  try {
    const [history, liquidity] = await Promise.all([
      Overseer.requestJSON(
        `/api/funding-history-multi?${new URLSearchParams({ asset, hours })}`,
        { forceRefresh },
      ),
      Overseer.requestJSON(`/api/liquidity-history-multi?${qs}`, { forceRefresh }),
    ]);
    if (request !== historyRequest) return;
    lastHistoryData = history;
    lastOiData = liquidity;
    renderHistoryChart();
    fundingState.ready('history');
  } catch (error) {
    if (request === historyRequest) {
      fundingState.error('Funding history', loadHistory, 'history');
    }
  } finally {
    if (historyLoadKey === loadKey) historyLoadKey = null;
  }
}

// Lightweight Charts has no native stacked-histogram type. The standard
// workaround: one full-opacity series per venue, each plotting the RUNNING
// CUMULATIVE total through that venue (in a fixed order), added back-to-front
// — largest cumulative (last venue) first, smallest (first venue's own raw
// value) last. Each later series draws on top and fully covers the bottom of
// the one behind it, so only the *difference* — that venue's own segment —
// stays visible. Net effect: a real stacked bar, bottom segment = first
// venue in VENUE_COLORS order, top segment = last, at solid color (no alpha
// blending needed once segments no longer overlap).
// Returns the bar series and the stacked total per bar (the open-interest sum).
function renderOiStack(chart, oiData, venueOrder) {
  const venues = venueOrder.filter(v => oiData[v] && oiData[v].length);
  if (!venues.length) return { handles: [], total: [] };

  const byTime = new Map();      // time -> { venue: oi_notional }
  for (const venue of venues) {
    for (const p of oiData[venue]) {
      if (!byTime.has(p.time)) byTime.set(p.time, {});
      byTime.get(p.time)[venue] = p.oi_notional;
    }
  }
  const times = [...byTime.keys()].sort((a, b) => a - b);

  const cumSeries = venues.map(() => []);
  for (const t of times) {
    const row = byTime.get(t);
    let running = 0;
    venues.forEach((venue, i) => {
      if (row[venue] != null) running += row[venue];
      cumSeries[i].push({ time: t, value: running });
    });
  }

  const handles = [];
  for (let i = venues.length - 1; i >= 0; i--) {
    const series = chart.addHistogramSeries({
      color: colorFor(venues[i]), priceScaleId: 'oi',
      priceFormat: { type: 'volume' },
      lastValueVisible: false, priceLineVisible: false,
    });
    series.setData(cumSeries[i]);
    handles.push(series);
  }
  return { handles, total: cumSeries[venues.length - 1] };
}

function updateHistoryHelp() {
  const rateLabel = rateMode === 'apr' ? 'Lines: annualised funding' : "Lines: each venue's rate per period";
  const smoothingLabel = smoothMode === 'ewma' ? 'EWMA-smoothed per venue' : 'raw settlements';
  document.getElementById('history-help').innerHTML = [
    rateLabel, smoothingLabel, 'Bars: open interest', 'Legend: latest raw settlement, or the hovered value',
  ].map(text => `<span>${text}</span>`).join('');
}

function renderHistoryChart() {
  const legendElement = document.getElementById('hlegend');
  const focusedVenue = legendElement.contains(document.activeElement)
    ? document.activeElement.closest('[data-venue]')?.dataset.venue
    : null;
  const chart = ensureHistoryChart();
  hHandles.forEach(h => chart.removeSeries(h));
  hOiHandles.forEach(h => chart.removeSeries(h));
  hHandles = []; hOiHandles = [];
  const data = lastHistoryData || {};
  const oiData = lastOiData || {};

  const legend = [];
  const responseVenues = Object.keys(data);
  const venueOrder = [
    ...VENUE_ORDER.filter(venue => responseVenues.includes(venue)),
    ...responseVenues.filter(venue => !VENUE_ORDER.includes(venue)).sort(),
  ];
  const visibleVenues = [];
  for (const venue of venueOrder) {
    const pts = data[venue];
    if (!pts || !pts.length) continue;
    const color = colorFor(venue);
    const label = pts[0].venue_label || venue;
    const visible = !hiddenVenues.has(venue);
    const lastPoint = pts[pts.length - 1];
    const last = historyRate(lastPoint);
    const linePoints = smoothMode === 'ewma'
      ? ewma(pts)
      : pts.map(point => ({ time: point.time, value: historyRate(point) }));
    if (visible) {
      const line = chart.addLineSeries({
        color, lineWidth: 2, priceScaleId: 'right',
        priceLineVisible: false, lastValueVisible: false,
        priceFormat: chartPercentFormat(rateMode === 'period' ? 4 : 1, true),
      });
      line.setData(linePoints);
      hHandles.push(line);
      visibleVenues.push(venue);
    }
    legend.push({
      venue,
      label,
      color,
      last,
      points: linePoints,
      intervalHours: lastPoint.interval_hours,
      visible,
    });
  }
  const oi = renderOiStack(chart, oiData, visibleVenues);
  hOiHandles = oi.handles;
  if (hOiHandles.length) {
    chart.priceScale('oi').applyOptions({ scaleMargins: { top: 0.75, bottom: 0 } });
  }
  const entries = legend.map(e => ({
    label: e.label, color: e.color, shape: 'line',
    title: `${e.visible ? 'Hide' : 'Show'} ${e.label}; Latest is the latest raw settlement`,
    text: time => time === null
      ? historyRateText(e.last, e.intervalHours)
      : historyRateText(valueAtOrBefore(e.points, time)),
  }));
  if (oi.total.length) {
    const latestTotal = oi.total[oi.total.length - 1].value;
    entries.push({
      label: 'Open interest', color: CHART_THEME.text, shape: 'bar',
      text: (time, param) => Fmt.usdCompact(time === null
        ? latestTotal
        : param?.seriesData.get(hOiHandles[0])?.value),
    });
  }
  hReadout.set(entries);
  // Venue entries double as show/hide toggles.
  legend.forEach((e, index) => {
    const item = legendElement.children[index];
    item.dataset.venue = e.venue;
    item.classList.add('funding-legend-toggle');
    item.setAttribute('role', 'button');
    item.tabIndex = 0;
    item.setAttribute('aria-pressed', String(e.visible));
  });
  legendElement.classList.toggle('hidden', !legend.length);
  const hours = document.getElementById('hhours');
  const asset = document.getElementById('hcoin').value;
  const windowLabel = hours.options[hours.selectedIndex]?.text || '';
  document.getElementById('history-subtitle').textContent = asset ? `${asset} · ${windowLabel}` : '';
  document.getElementById('hempty-title').textContent =
    `No funding history for ${asset || 'this asset'} in ${windowLabel}.`;
  document.getElementById('hempty').classList.toggle('hidden', legend.length > 0);
  if (focusedVenue) {
    [...legendElement.querySelectorAll('[data-venue]')]
      .find(button => button.dataset.venue === focusedVenue)?.focus({ preventScroll: true });
  }
  chart.timeScale().fitContent();
}

function toggleHistoryVenue(btn) {
  const venue = btn.dataset.venue;
  hiddenVenues.has(venue) ? hiddenVenues.delete(venue) : hiddenVenues.add(venue);
  fundingFilters.setList('venues', hiddenVenues);
  renderHistoryChart();
}
const historyLegend = document.getElementById('hlegend');
historyLegend.addEventListener('click', e => {
  const btn = e.target.closest('[data-venue]');
  if (btn) toggleHistoryVenue(btn);
});
historyLegend.addEventListener('keydown', e => {
  const btn = e.target.closest('[data-venue]');
  if (!btn || (e.key !== 'Enter' && e.key !== ' ')) return;
  e.preventDefault();
  toggleHistoryVenue(btn);
});

function setSmooth(mode, redraw = true) {
  smoothMode = mode;
  fundingFilters.set('smoothing', mode);
  document.getElementById('smooth-raw').setAttribute('aria-pressed', String(mode === 'raw'));
  document.getElementById('smooth-ewma').setAttribute('aria-pressed', String(mode === 'ewma'));
  updateHistoryHelp();
  if (redraw) renderHistoryChart();
}
document.getElementById('smooth-raw').addEventListener('click', () => setSmooth('raw'));
document.getElementById('smooth-ewma').addEventListener('click', () => setSmooth('ewma'));

for (const id of ['hhours', 'hbucket']) {
  fundingFilters.restoreSelect(document.getElementById(id));
}
document.getElementById('hcoin').addEventListener('change', event => {
  fundingFilters.set('asset', event.target.value);
  loadHistory();
});
document.getElementById('hhours').addEventListener('change', event => {
  fundingFilters.set('hhours', event.target.value);
  const suggested = DEFAULT_BUCKET[event.target.value];
  if (suggested) {
    document.getElementById('hbucket').value = suggested;
    fundingFilters.set('hbucket', suggested);
  }
  loadHistory();
});
document.getElementById('hbucket').addEventListener('change', event => {
  fundingFilters.set('hbucket', event.target.value);
  loadHistory();
});

function setView(view) {
  activeView = view;
  fundingFilters.set('view', view);
  const isTable = view === 'table';
  document.getElementById('view-table').classList.toggle('hidden', !isTable);
  if (isTable) fundingState.ready('history');
  document.getElementById('view-history').classList.toggle('hidden', isTable);
  document.getElementById('tab-table').setAttribute('aria-selected', String(isTable));
  document.getElementById('tab-history').setAttribute('aria-selected', String(!isTable));
  document.getElementById('tab-table').tabIndex = isTable ? 0 : -1;
  document.getElementById('tab-history').tabIndex = isTable ? -1 : 0;
  if (!isTable && lastFundingData) {
    populateCoinSelector(lastFundingData);
    loadHistory();
  }
}
const fundingViewTabs = [
  document.getElementById('tab-table'),
  document.getElementById('tab-history'),
];
fundingViewTabs.forEach((tab, index) => {
  tab.addEventListener('click', () => setView(tab.dataset.view));
  tab.addEventListener('keydown', event => {
    let nextIndex = null;
    if (event.key === 'ArrowRight') nextIndex = (index + 1) % fundingViewTabs.length;
    if (event.key === 'ArrowLeft') nextIndex = (index - 1 + fundingViewTabs.length) % fundingViewTabs.length;
    if (event.key === 'Home') nextIndex = 0;
    if (event.key === 'End') nextIndex = fundingViewTabs.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    const nextTab = fundingViewTabs[nextIndex];
    nextTab.focus();
    setView(nextTab.dataset.view);
  });
});
setSmooth(smoothMode, false);
setRateMode(rateMode, false);
setView(activeView);
})();
