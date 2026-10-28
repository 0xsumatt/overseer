(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const labels = {hyperliquid: VENUE_LABELS.hyperliquid, lighter: VENUE_LABELS.lighter, bulk: VENUE_LABELS.bulk, risex: VENUE_LABELS.rise};
  const exchangeLogos = JSON.parse($('positions-watchlist-sticky').dataset.exchangeLogos);
  const storageKey = 'overseer.positions.watchlist.v1';
  const accounts = [];
  let snapshot = null;
  let screenOffline = false;
  let visibilityVersion = 0;
  let dataVersion = 0;
  let snapshotVersion = -1;
  let aggregateDirty = false;
  let aggregateBusy = false;
  let aggregateNextAt = 0;
  let aggregateFailures = 0;
  let aggregateController = null;
  let activeRead = null;
  let nextReadAt = 0;
  let fx = null;
  let fxBusy = false;
  let fxNextAt = 0;
  let fxError = null;
  // Discovery is cancellable; no account is committed until its venue finishes every page.
  let resolving = false;
  let editingId = null;
  const exchangeInputs = [...document.querySelectorAll('#positions-add-form input[name="venues"]')];
  let chosenVenues = new Set(['hyperliquid', 'risex']);
  let entryVersion = 0;
  let entrySubmission = null;
  let localError = null;
  let watchlistPinned = false;
  let watchlistExpandedHeight = 0;
  let watchlistFrame = null;
  let watchlistNeedsMeasure = false;
  const node = (tag, text, className) => {
    const el = document.createElement(tag);
    if (text !== undefined && text !== null) el.textContent = text;
    if (className) el.className = className;
    return el;
  };
  const badge = (text, tone = '') => {
    const el = node('span', text, 'badge');
    if (tone) el.dataset.tone = tone;
    return el;
  };
  function emptyState(title, hint, className = '') {
    const el = node('div', undefined, `empty-state${className ? ' ' + className : ''}`);
    el.append(node('p', title, 'empty-state-title'), node('p', hint, 'empty-state-hint'));
    return el;
  }
  // Presentation only: supplied financial strings never change in API payloads.
  function decimalParts(value) {
    if (value === null || value === undefined || value === '') return null;
    const match = /^(-?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(String(value));
    if (!match) return null;
    const exponent = Number(match[4] || 0);
    if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 100) return null;
    const digits = match[2] + (match[3] || '');
    const point = match[2].length + exponent;
    const whole = (point <= 0 ? '0' : point >= digits.length ? digits + '0'.repeat(point - digits.length) : digits.slice(0, point)).replace(/^0+(?=\d)/, '');
    const fraction = (point <= 0 ? '0'.repeat(-point) + digits : point >= digits.length ? '' : digits.slice(point)).replace(/0+$/, '');
    return {negative: Boolean(match[1]), nonzero: /[1-9]/.test(digits), whole, fraction};
  }
  function amount(value, signed = false) {
    const parts = decimalParts(value);
    if (!parts) return '—';
    const {whole, fraction, nonzero, negative} = parts;
    const sign = nonzero ? negative ? '−' : signed ? '+' : '' : '';
    if (whole.length > 12 || (whole === '0' && fraction && fraction.search(/[1-9]/) >= 4)) {
      const first = whole === '0' ? fraction.search(/[1-9]/) : 0;
      const digits = whole === '0' ? fraction.slice(first) : whole + fraction;
      const exponent = whole === '0' ? -(first + 1) : whole.length - 1;
      const tail = digits.slice(1, 5).replace(/0+$/, '');
      const approximate = /[1-9]/.test(digits.slice(5));
      return `${approximate ? '~' : ''}${sign}${digits[0]}${tail ? '.' + tail : ''}e${exponent}`;
    }
    const places = whole.length >= 4 ? 2 : 4;
    const tail = fraction.slice(0, places).replace(/0+$/, '');
    return `${/[1-9]/.test(fraction.slice(places)) ? '~' : ''}${sign}${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}${tail ? '.' + tail : ''}`;
  }
  function numberValue(value, currency = '', signed = false, estimated = false) {
    const parts = decimalParts(value);
    if (!parts) return node('span', 'Unavailable', 'positions-muted');
    const el = node('details', undefined, 'positions-number');
    el.dataset.sign = !parts.nonzero ? 'zero' : parts.negative ? 'negative' : 'positive';
    const formatted = amount(value, signed);
    const display = `${formatted}${currency ? ' ' + currency : ''}`;
    const summary = node('summary');
    summary.append(node('span', formatted, 'positions-number-amount'));
    if (currency) summary.append(node('span', currency, 'positions-number-unit'));
    summary.setAttribute('aria-label', `${display}; expand for ${estimated ? 'current-FX estimate before display shortening' : 'exact supplied amount'}`);
    el.append(summary, node('div', `${estimated ? 'Current-FX estimate before display shortening (approximate)' : 'Exact'}: ${String(value)}${currency ? ' ' + currency : ''}`, 'positions-exact'));
    return el;
  }
  function usd(value) {
    return numberValue(value, 'USD', true);
  }
  function buckets(values, empty = 'None reported') {
    if (!values?.length) return node('span', empty, 'positions-muted');
    const el = node('div', undefined, 'positions-buckets');
    for (const value of values) el.append(numberValue(value.amount, value.currency || 'Unknown currency', true));
    return el;
  }
  function currentEstimate(values) {
    if (!values?.length) return node('span', 'Unavailable', 'positions-muted');
    let sum = 0;
    for (const value of values) {
      const rate = snapshot?.fx?.rates?.[value.currency];
      if (rate === null || rate === undefined || !decimalParts(value.amount)) return node('span', 'Unavailable', 'positions-muted');
      const product = Number(value.amount) * Number(rate);
      if (!Number.isFinite(product) || (product === 0 && decimalParts(value.amount).nonzero && decimalParts(rate)?.nonzero)) return node('span', 'Unavailable', 'positions-muted');
      sum += product;
    }
    return Number.isFinite(sum) ? numberValue(String(sum), 'USD', true, true) : node('span', 'Unavailable', 'positions-muted');
  }
  function side(value) {
    const parts = decimalParts(value);
    return !parts ? 'Unknown' : !parts.nonzero ? 'Flat' : parts.negative ? 'Short' : 'Long';
  }
  function disclosure(title, content, className = 'disclosure') {
    const el = node('details', undefined, className);
    const body = node('div', undefined, 'disclosure-body');
    body.append(content);
    el.append(node('summary', title), body);
    return el;
  }
  function field(title, value) {
    const el = node('div', undefined, 'positions-inline-metric');
    el.append(node('span', title, 'positions-muted'), typeof value === 'string' ? node('span', value) : value);
    return el;
  }
  function age(ts) {
    const time = Date.parse(ts);
    return Number.isFinite(time) ? `${Fmt.age((Date.now() - time) / 1000)} ago` : 'Age unknown';
  }
  function renderReadFreshness(items) {
    $('positions-funding-read-age').hidden = !items.length;
    for (const kind of ['positions', 'funding']) {
      let oldest = null, oldestTime = Infinity, unread = 0, failed = 0;
      let retained = false, partial = false;
      for (const account of items) {
        const reading = account[kind];
        const time = Date.parse(reading?.checked_at);
        if (!Number.isFinite(time)) unread += 1;
        else {
          if (time < oldestTime) { oldest = reading.checked_at; oldestTime = time; }
          retained ||= reading.state !== 'fresh';
        }
        if (reading?.state === 'error') failed += 1;
        partial ||= kind === 'funding' && (reading?.catching_up || reading?.state === 'fresh' && !reading.complete);
      }
      // "12s ago" for positions (the bar's label says Readings) and "Funding 3m
      // ago": short enough for one line on a phone. "Oldest" is in the tooltip.
      const details = [!items.length ? 'No accounts' : oldest ? age(oldest) : partial ? 'Awaiting complete read' : failed ? 'No successful reads' : 'Awaiting reads'];
      if (oldest && unread) details.push(`${unread} unread`);
      if (failed) details.push(`${failed} ${failed === 1 ? 'error' : 'errors'}`);
      if (retained) details.push('retained');
      if (partial) details.push('partial');
      const el = $(`positions-${kind}-read-age`);
      const lead = details.shift();
      const first = kind === 'positions' ? lead : `Funding ${lead.charAt(0).toLowerCase()}${lead.slice(1)}`;
      const text = [first, ...details].join(' · ');
      if (el.textContent !== text) el.textContent = text;
      el.classList.toggle('positions-error', failed > 0);
      el.classList.toggle('positions-warning', !failed && Boolean(unread || retained || partial));
      el.title = oldest ? `Oldest successful ${kind} read: ${Fmt.dateTime(oldest)}` : `No successful ${kind} read timestamp`;
    }
  }
  function timestamp(ts) {
    const el = node('span');
    if (ts) { el.dataset.positionsAge = ts; el.title = Fmt.dateTime(ts); el.textContent = age(ts); }
    else el.textContent = 'never successfully read';
    return el;
  }
  function status(label, state) {
    const line = node('div', undefined, 'positions-account-state');
    line.dataset.positionStatusKind = label === 'Positions' ? 'positions' : 'funding';
    const data = state || {state: 'pending'};
    const tone = data.state === 'error' ? 'error' : data.state === 'fresh' ? '' : 'warning';
    line.append(badge(`${label}: ${data.state || 'pending'}`, tone), timestamp(data.checked_at));
    if (data.state !== 'fresh') line.append(node('span', data.checked_at ? 'Retained / not current' : 'Unknown, not zero', 'positions-warning'));
    if (data.error) line.append(node('p', data.error, 'positions-error'));
    if (data.catching_up) line.append(node('span', 'Catching up · known subtotal', 'positions-warning'));
    if (label === 'Settled funding' && !data.complete && !data.catching_up) line.append(node('span', 'Partial history · known subtotal', 'positions-warning'));
    if (data.coverage_note) line.append(node('p', data.coverage_note, 'positions-warning'));
    if (data.attempted_at || data.coverage_since) {
      const coverage = node('div', undefined, 'positions-muted');
      if (data.attempted_at) { coverage.append(node('span', 'Last attempted '), timestamp(data.attempted_at)); }
      if (data.coverage_since) coverage.append(node('p', `Funding coverage starts ${data.coverage_since}`));
      line.append(disclosure('Read / coverage details', coverage));
    }
    return line;
  }
  function notice(text, state = '') {
    $('positions-notice').textContent = text;
    if (state) $('positions-notice').dataset.tone = state;
    else delete $('positions-notice').dataset.tone;
  }
  function metric(title, value, note, kind, status) {
    const el = node('div', undefined, 'kpi positions-metric');
    el.dataset.kind = kind;
    const header = node('div', undefined, 'positions-metric-heading');
    header.append(node('span', title, 'kpi-label'), badge(status, status.startsWith('Partial') ? 'warning' : ''));
    const reading = node('div', undefined, 'kpi-value positions-metric-value');
    reading.append(typeof value === 'string' ? node('span', value) : value);
    const footer = node('div', undefined, 'kpi-meta positions-metric-note');
    footer.append(typeof note === 'string' ? node('p', note) : note);
    el.append(header, reading, footer);
    return el;
  }
  function markRetainedScreen() {
    document.querySelector('[data-positions-offline]')?.remove();
    if (!screenOffline) return;
    const retained = node('div', 'Screen offline · retained in memory — do not treat these totals as current.', 'notice positions-offline');
    retained.dataset.tone = 'error';
    retained.dataset.positionsOffline = '';
    $('positions-summary').before(retained);
  }
  function renderTotals(data) {
    const totals = data.totals || {};
    const empty = !data.accounts.length;
    const exposureKnown = !empty && totals.positions_complete;
    const fundingKnown = !empty && totals.funding_complete;
    const anyPositionRead = data.accounts.some(account => account.positions?.checked_at);
    const anyFundingRead = data.accounts.some(account => account.funding?.checked_at);
    const fundingFx = field('Current-FX estimate', empty || !anyFundingRead ? 'Unavailable' : usd(totals.funding_usd));
    const pnlFx = field('Current-FX estimate', empty ? 'Unavailable' : currentEstimate(totals.unrealized_pnl));
    $('positions-summary').replaceChildren(
      metric('Unmatched exposure', empty ? 'No accounts' : anyPositionRead ? usd(totals.net_delta_usd) : 'Awaiting snapshot', exposureKnown ? 'Absolute net deltas per asset' : 'Partial / unknown portfolio', 'exposure', empty ? 'Waiting' : exposureKnown ? 'USD estimate' : 'Partial'),
      metric('Settled funding', empty ? 'No accounts' : buckets(totals.funding, fundingKnown ? 'No settled payments in period' : 'Unknown · no payments available'), fundingFx, 'funding', empty ? 'Waiting' : fundingKnown ? 'Native' : 'Partial subtotal'),
      metric('Unrealised P&L', empty ? 'No accounts' : buckets(totals.unrealized_pnl, 'Unavailable'), pnlFx, 'pnl', empty ? 'Waiting' : exposureKnown ? 'Native' : 'Partial subtotal')
    );
    markRetainedScreen();
    const target = $('positions-fx');
    target.replaceChildren(node('span', `Current FX source: ${data.fx?.source || 'Unavailable'} · updated `), timestamp(data.fx?.checked_at));
    if (fxError) target.append(node('span', ` · ${fxError}`, 'positions-warning'));
  }
  function detail(grid, title, value) {
    const pair = node('div');
    const reading = node('dd');
    reading.append(typeof value === 'string' ? node('span', value) : value);
    pair.append(node('dt', title), reading);
    grid.append(pair);
  }
  function scanValue(value, currency = '', signed = false) {
    const parts = decimalParts(value);
    if (!parts) return node('span', 'Unavailable', 'positions-muted');
    const el = node('span', undefined, 'positions-scan-number');
    el.dataset.sign = !parts.nonzero ? 'zero' : parts.negative ? 'negative' : 'positive';
    el.append(node('span', amount(value, signed), 'positions-number-amount'));
    if (currency) el.append(' ', node('span', currency, 'positions-number-unit'));
    return el;
  }
  function scanBuckets(values, empty) {
    if (!values?.length) return node('span', empty, 'positions-muted');
    const el = node('span', undefined, 'positions-scan-values');
    for (const value of values) el.append(scanValue(value.amount, value.currency || 'Unknown currency', true));
    return el;
  }
  function scanMetric(title, reading, kind) {
    const el = node('span', undefined, 'positions-scan-metric');
    el.dataset.kind = kind;
    el.append(node('span', title, 'positions-scan-label'), reading);
    return el;
  }
  function renderAssets(data) {
    const target = $('positions-assets');
    const opened = new Set([...target.querySelectorAll('details[data-asset][open]')].map(el => el.dataset.asset));
    target.replaceChildren();
    if (!data.assets.length) {
      target.append(!data.accounts.length
        ? emptyState('No accounts in this tab', 'Add an account to read exposure. No accounts means unknown, not flat.')
        : data.totals?.positions_complete
          ? emptyState('No open positions', 'No open positions or settled payments in this period.')
          : emptyState('Awaiting complete readings', 'Exposure and funding are unknown, not zero.'));
      return;
    }
    const listHeading = node('div', undefined, 'list-head positions-list-head');
    listHeading.setAttribute('aria-hidden', 'true');
    for (const title of ['', 'Asset / reading quality', 'Net delta, USD', 'Funding, native', 'Unrealised P&L, native']) listHeading.append(node('span', title));
    target.append(listHeading);
    for (const asset of data.assets) {
      const card = node('details', undefined, 'positions-asset');
      card.dataset.asset = asset.asset;
      card.dataset.state = asset.partial ? 'partial' : 'complete';
      card.open = opened.has(asset.asset);
      let retained = false;
      let stale = false;
      for (const leg of asset.legs || []) {
        const funding = data.accounts.find(account => String(account.id) === String(leg.account_id))?.funding;
        retained ||= Boolean((leg.checked_at && leg.state !== 'fresh') || (funding?.checked_at && funding.state !== 'fresh'));
        stale ||= leg.state === 'stale' || funding?.state === 'stale';
      }
      const direction = side(asset.net_quantity);
      const identity = node('span', undefined, 'positions-asset-identity');
      const name = node('span', undefined, 'positions-asset-name');
      const quality = stale ? 'Stale / retained' : asset.partial ? `Partial${retained ? ' / retained' : ''}` : retained ? 'Delayed / retained' : 'Current';
      name.append(
        node('span', asset.label || asset.asset, 'positions-asset-title'),
        badge(quality, asset.partial || retained ? 'warning' : '')
      );
      identity.append(name, node('span', `${direction} · ${amount(asset.net_quantity, true)}`, `positions-asset-net ${direction === 'Long' ? 'positions-long' : direction === 'Short' ? 'positions-short' : 'positions-muted'}`));
      const heading = node('summary', undefined, 'positions-asset-heading');
      heading.append(
        identity,
        scanMetric('Net delta, USD', scanValue(asset.net_delta_usd, 'USD', true), 'delta'),
        scanMetric(`Funding${data.totals?.funding_complete ? '' : ' · partial'}`, scanBuckets(asset.funding, data.totals?.funding_complete ? 'No payments' : 'Unavailable'), 'funding'),
        scanMetric(`Unrealised P&L${data.totals?.positions_complete ? '' : ' · partial'}`, scanBuckets(asset.unrealized_pnl, 'Unavailable'), 'pnl')
      );
      const body = node('div', undefined, 'positions-details');
      const overview = node('div', undefined, 'positions-asset-overview');
      const line = node('div', undefined, 'positions-asset-line');
      line.append(field('Net base', numberValue(asset.net_quantity, asset.asset, true)), field('Long', numberValue(asset.long_quantity, asset.asset)), field('Short', numberValue(asset.short_quantity, asset.asset)), field('Net delta estimate', usd(asset.net_delta_usd)));
      overview.append(line);
      const totals = node('div', undefined, 'positions-asset-line');
      totals.append(field('Unrealised P&L, native', buckets(asset.unrealized_pnl, 'Unavailable')), field(`Funding, ${data.totals?.funding_complete ? 'native' : 'partial subtotal'}`, buckets(asset.funding, data.totals?.funding_complete ? 'No settled payments' : 'Unavailable / incomplete')));
      overview.append(totals);
      const estimates = node('dl', undefined, 'positions-leg-grid');
      detail(estimates, 'USD reference', numberValue(asset.reference_price_usd, 'USD'));
      detail(estimates, 'Unrealised P&L, current-FX USD estimate', currentEstimate(asset.unrealized_pnl));
      detail(estimates, 'Funding, current-FX USD estimate', usd(asset.funding_usd));
      overview.append(disclosure('USD reference & estimates', estimates));
      body.append(node('p', 'Scroll sideways for size, prices and funding.', 'positions-table-hint positions-muted'));
      const scroll = node('div', undefined, 'positions-table-scroll table-card scroll-thin');
      scroll.tabIndex = 0; scroll.setAttribute('role', 'region'); scroll.setAttribute('aria-label', `${asset.label || asset.asset} account legs; horizontally scrollable table`);
      const table = node('table', undefined, 'data-table positions-table');
      table.append(node('caption', 'Native amounts · expand a value for exact precision'));
      const thead = node('thead');
      const headers = node('tr');
      for (const [index, title] of ['Venue / account', 'Side', 'Base size', 'Entry, quote', 'Mark, quote', 'Unrealised P&L, native', 'Settled funding, native', 'Positions freshness'].entries()) {
        const th = node('th', title, index >= 2 && index <= 6 ? 'num' : undefined); th.scope = 'col'; headers.append(th);
      }
      thead.append(headers); table.append(thead);
      const rows = node('tbody');
      for (const leg of asset.legs || []) {
        const account = data.accounts.find(item => String(item.id) === String(leg.account_id));
        const funding = account?.funding;
        const row = node('tr');
        const identity = node('th'); identity.scope = 'row';
        identity.append(node('span', leg.venue_label || labels[leg.venue] || leg.venue, 'positions-venue-name'), node('span', leg.account_label, 'positions-leg-account'));
        const metadata = node('dl', undefined, 'positions-leg-metadata');
        detail(metadata, 'Address', leg.address || 'Unknown');
        detail(metadata, 'Market / scope', `${leg.symbol || asset.asset} / ${leg.scope || 'Unknown'}`);
        detail(metadata, 'Quote / settlement', `${leg.quote_currency || 'Unknown'} / ${leg.settlement_currency || 'Unknown'}`);
        detail(metadata, 'Eligible collateral', leg.collateral_currencies?.join(', ') || 'Unknown');
        detail(metadata, 'Funding attribution', leg.funding_scope === 'account-market' ? 'Account + market; historical margin mode unavailable' : leg.funding_scope || leg.scope || 'Unknown');
        identity.append(disclosure('Scope & collateral', metadata));
        row.append(identity);
        const direction = side(leg.quantity);
        const sideCell = node('td', direction, direction === 'Long' ? 'positions-long' : direction === 'Short' ? 'positions-short' : 'positions-muted');
        row.append(sideCell);
        for (const reading of [
          numberValue(leg.quantity, asset.asset, true),
          numberValue(leg.entry_price, leg.quote_currency || 'Unknown currency'),
          numberValue(leg.mark_price, leg.quote_currency || 'Unknown currency'),
          numberValue(leg.unrealized_pnl, leg.unrealized_pnl_currency || leg.settlement_currency || 'Unknown currency', true)
        ]) { const cell = node('td', undefined, 'num'); cell.append(reading); row.append(cell); }
        const fundingCell = node('td', undefined, 'num');
        fundingCell.append(buckets(leg.funding, funding?.complete ? 'No settled payments' : 'Unavailable / incomplete'));
        fundingCell.append(node('span', `Funding: ${funding?.state || 'Unknown'}${funding?.complete ? '' : ' · partial'}`, funding?.state === 'error' ? 'positions-error positions-cell-state' : funding?.state === 'fresh' && funding.complete ? 'positions-muted positions-cell-state' : 'positions-warning positions-cell-state'));
        if (funding?.state !== 'fresh' && funding?.checked_at) fundingCell.append(node('span', 'Retained', 'positions-warning positions-cell-state'));
        row.append(fundingCell);
        const freshness = node('td', undefined, 'positions-freshness');
        freshness.append(badge(leg.state || 'Unknown', leg.state === 'error' ? 'error' : leg.state === 'fresh' ? '' : 'warning'), timestamp(leg.checked_at));
        if (leg.state !== 'fresh') freshness.append(node('span', leg.checked_at ? 'Retained / not current' : 'Unknown, not zero', 'positions-warning positions-cell-state'));
        row.append(freshness); rows.append(row);
      }
      table.append(rows); scroll.append(table); body.append(scroll, disclosure('Asset totals & valuation', overview));
      card.append(heading, body); target.append(card);
    }
  }
  function canonicalAddress(venue, raw) {
    if (!Object.hasOwn(labels, venue) || typeof raw !== 'string') throw new Error('Choose a supported venue and account address.');
    const address = raw.trim();
    if (venue === 'hyperliquid' || venue === 'risex') {
      if (!/^0x[0-9a-fA-F]{40}$/.test(address)) throw new Error('Use the account’s 0x address (40 hexadecimal characters).');
      return address.toLowerCase();
    }
    if (venue === 'lighter') {
      if (!/^[0-9]{1,15}$/.test(address) || Number(address) > 2 ** 48 - 1) throw new Error('Lighter account identities are numeric indices. Add a wallet to discover its owned accounts automatically.');
      return String(Number(address));
    }
    if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(address)) throw new Error('BULK requires a Solana-style base58 account public key.');
    const alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
    let value = 0n;
    for (const char of address) value = value * 58n + BigInt(alphabet.indexOf(char));
    const leading = address.match(/^1*/)[0].length;
    const bytes = value === 0n ? 0 : Math.ceil(value.toString(2).length / 8);
    if (leading + bytes !== 32) throw new Error('BULK account public key must decode to 32 bytes.');
    return address;
  }
  function validLabel(raw) {
    const label = raw.trim();
    if (!label || [...label].length > 80 || /[\x00-\x1f]/.test(label)) throw new Error('Use an account label of 1–80 characters without control characters.');
    return label;
  }
  function validToken(raw, address) {
    const token = raw.trim();
    const match = /^ro:(\d+):(single|all):(\d+):([0-9a-fA-F]{16,})$/.exec(token);
    if (token.length > 2048 || !match) throw new Error('Only official Lighter ro: read-only tokens are accepted; never enter trading credentials.');
    if (match[2] === 'single' && address !== undefined && BigInt(match[1]) !== BigInt(address)) throw new Error('This single-account token belongs to another Lighter account index.');
    if (BigInt(match[3]) <= BigInt(Math.floor(Date.now() / 1000))) throw new Error('This read-only token has expired. Create a replacement at Lighter.');
    return token;
  }
  function memoryAccount(saved) {
    return {...saved, readOnlyToken: '', generation: 0, fundingGeneration: 0, snapshot: null,
      positions_checked_at: null, positions_attempted_at: null, positions_error: null,
      funding_checked_at: null, funding_attempted_at: null, funding_error: null,
      funding_since: null, funding_coverage_note: null, fundingScan: null, fundingComplete: false,
      events: new Map(), cursors: new Set(), positionsFailures: 0, fundingFailures: 0,
      nextPositionsAt: 0, nextFundingAt: 0};
  }
  function persistWatchlist() {
    try {
      if (!accounts.length) sessionStorage.removeItem(storageKey);
      else sessionStorage.setItem(storageKey, JSON.stringify({version: 1, accounts: accounts.map(({id, venue, address, label, created_at}) => ({id, venue, address, label, created_at}))}));
      $('positions-storage-message').textContent = 'Only this tab’s addresses, labels and local account identifiers use sessionStorage, with no login or server database association. They survive refresh; browser session restoration may retain them. Other fresh tabs are independent. Tokens and financial readings stay only in page memory.';
      $('positions-storage-message').className = '';
    } catch (_) {
      $('positions-storage-message').textContent = 'Tab storage is unavailable or full. The latest watchlist change exists only in page memory and may not survive refresh; an older tab list may remain. No tokens or financial readings are saved.';
      $('positions-storage-message').className = 'positions-error';
    }
  }
  function restoreWatchlist() {
    try {
      const raw = sessionStorage.getItem(storageKey);
      if (!raw) return;
      const saved = JSON.parse(raw);
      if (saved?.version !== 1 || !Array.isArray(saved.accounts) || saved.accounts.length > 32) throw new Error('Invalid tab watchlist.');
      const restored = [];
      const ids = new Set();
      const addresses = new Set();
      for (const item of saved.accounts) {
        if (!item || typeof item.id !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(item.id) || typeof item.label !== 'string' || typeof item.created_at !== 'string' || !Number.isFinite(Date.parse(item.created_at))) throw new Error('Invalid tab account.');
        const address = canonicalAddress(item.venue, item.address);
        const key = `${item.venue}:${address}`;
        if (ids.has(item.id) || addresses.has(key)) throw new Error('Duplicate tab account.');
        ids.add(item.id); addresses.add(key);
        restored.push(memoryAccount({id: item.id, venue: item.venue, address, label: validLabel(item.label), created_at: item.created_at}));
      }
      accounts.push(...restored);
      persistWatchlist();
    } catch (_) {
      $('positions-storage-message').textContent = 'The tab watchlist could not be restored. No saved financial readings or tokens were loaded. Clear the watchlist to remove inaccessible or invalid tab data, or add accounts again.';
      $('positions-storage-message').className = 'positions-error';
    }
  }
  async function api(path, body, controller = new AbortController()) {
    const encoded = body === undefined ? undefined : JSON.stringify(body);
    const limit = path === '/api/positions/aggregate' ? 8 * 1024 * 1024 : 64 * 1024;
    if (path === '/api/positions/aggregate' && body.events.length > 50000) throw new Error('Funding history exceeds the 50,000-event summary limit. All received rows remain in page memory, but exact totals are unavailable; select a shorter period or remove accounts.');
    if (encoded && new TextEncoder().encode(encoded).length > limit) throw new Error('The request exceeds the safe size limit. Readings remain incomplete; choose a shorter funding period or fewer accounts. No rows were discarded.');
    const timeout = setTimeout(() => controller.abort(), 28000);
    try {
      const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', credentials: 'omit', cache: 'no-store', signal: controller.signal,
        ...(body === undefined ? {} : {headers: {'Content-Type': 'application/json'}, body: encoded})});
      const result = await response.json().catch(() => null);
      if (!response.ok) {
        const error = new Error(typeof result?.error === 'string' ? result.error : `Read-only request unavailable (${response.status}).`);
        error.safe = true;
        throw error;
      }
      if (!result || typeof result !== 'object') {
        const error = new Error('The read-only response is incomplete; last good readings remain in page memory.');
        error.safe = true;
        throw error;
      }
      return result;
    } catch (error) {
      if (error.safe) throw error;
      throw new Error('Read-only request failed or timed out. Last good readings remain only in page memory.');
    } finally { clearTimeout(timeout); }
  }
  function changed() {
    dataVersion += 1;
    aggregateDirty = Boolean(accounts.length);
  }
  function localAccounts() {
    return {accounts: accounts.map(account => {
      const state = kind => {
        const checked = account[`${kind}_checked_at`];
        const error = account[`${kind}_error`];
        const elapsed = checked ? Math.max(0, Date.now() - Date.parse(checked)) : Infinity;
        const [fresh, stale] = kind === 'positions' ? [120000, 300000] : [600000, 900000];
        const readingState = error ? 'error' : !checked ? 'pending' : elapsed <= fresh ? 'fresh' : elapsed <= stale ? 'delayed' : 'stale';
        return {state: readingState, checked_at: checked, attempted_at: account[`${kind}_attempted_at`], error,
          complete: ['fresh', 'delayed'].includes(readingState) && (kind === 'positions' ? Boolean(account.snapshot) : account.fundingComplete),
          ...(kind === 'funding' ? {catching_up: Boolean(account.fundingScan), coverage_since: account.funding_since, coverage_note: account.funding_coverage_note} : {})};
      };
      return {id: account.id, venue: account.venue, address: account.address, label: account.label, has_read_only_token: Boolean(account.readOnlyToken),
        positions: state('positions'), funding: state('funding'), balances: account.snapshot?.balances || [], equity: account.snapshot?.equity || [], notes: account.snapshot?.notes || []};
    })};
  }
  function maskScreen(text) {
    visibilityVersion += 1;
    aggregateController?.abort();
    snapshot = null; snapshotVersion = -1; screenOffline = false;
    aggregateNextAt = 0; aggregateFailures = 0;
    changed();
    if (!accounts.length) {
      const empty = {accounts: [], assets: [], totals: {}, fx: null};
      renderTotals(empty); renderAssets(empty);
      $('positions-fx').replaceChildren();
    } else {
      markRetainedScreen();
      $('positions-summary').replaceChildren(emptyState('Totals hidden', text, 'positions-wide positions-placeholder'));
      $('positions-assets').replaceChildren(emptyState('Updating summaries', 'Previous totals stay hidden until exact summaries for this watchlist and period arrive.'));
      $('positions-fx').replaceChildren();
    }
    renderAccounts(localAccounts(), true);
    notice(text, accounts.length ? 'warning' : '');
  }
  function clearFunding(account) {
    account.fundingGeneration += 1;
    if (activeRead?.account === account && activeRead.kind === 'funding') activeRead.controller.abort();
    account.events.clear(); account.cursors.clear(); account.fundingScan = null; account.fundingComplete = false;
    account.funding_checked_at = null; account.funding_attempted_at = null; account.funding_error = null;
    account.funding_since = null; account.funding_coverage_note = null; account.fundingFailures = 0; account.nextFundingAt = 0;
  }
  function replaceToken(account, token) {
    account.readOnlyToken = token;
    clearFunding(account);
  }
  function updateWatchlistGeometry() {
    // Reserve the expanded footprint so shrinking a pinned strip cannot move the page underneath it.
    const sticky = $('positions-watchlist-sticky');
    const height = sticky.getBoundingClientRect().height;
    if (sticky.dataset.compact !== 'true') watchlistExpandedHeight = height;
    const space = sticky.dataset.compact === 'true' ? Math.max(0, watchlistExpandedHeight - height) : 0;
    $('positions-app').style.setProperty('--positions-watchlist-height', `${height}px`);
    $('positions-watchlist-space').style.setProperty('--positions-watchlist-space', `${space}px`);
  }
  function updateWatchlistMode(remeasure = false) {
    const sticky = $('positions-watchlist-sticky');
    const top = $('positions-watchlist-anchor').getBoundingClientRect().top;
    if (top < -8) watchlistPinned = true;
    else if (top > 8) watchlistPinned = false;
    const keyboardInspecting = document.activeElement?.matches('.positions-watchlist-tile:focus-visible');
    const compact = watchlistPinned && accounts.length > 0 && !$('positions-add').open && $('positions-management').hidden && !keyboardInspecting;
    if (remeasure && sticky.dataset.compact === 'true') sticky.dataset.compact = 'false';
    if (compact && sticky.dataset.compact !== 'true') watchlistExpandedHeight = sticky.getBoundingClientRect().height;
    sticky.dataset.compact = String(compact);
    updateWatchlistGeometry();
  }
  function queueWatchlistMode(remeasure = false) {
    watchlistNeedsMeasure ||= remeasure;
    if (watchlistFrame !== null) return;
    watchlistFrame = requestAnimationFrame(() => {
      watchlistFrame = null;
      const measure = watchlistNeedsMeasure;
      watchlistNeedsMeasure = false;
      updateWatchlistMode(measure);
    });
  }
  function shortIdentity(account) {
    const prefix = account.venue === 'lighter' ? '#' : account.address.startsWith('0x') ? '0x' : '';
    const value = prefix === '0x' ? account.address.slice(2) : account.address;
    return prefix + (value.length > 6 ? `${value.slice(0, 3)}…${value.slice(-3)}` : value);
  }
  function syncWatchlistExpansion() {
    for (const tile of $('positions-watchlist-tiles').children) {
      const card = document.getElementById(tile.getAttribute('aria-controls'));
      tile.setAttribute('aria-expanded', String(!$('positions-management').hidden && Boolean(card?.open)));
    }
  }
  function renderWatchlistTiles(items) {
    const target = $('positions-watchlist-tiles');
    const existing = new Map([...target.children].map(tile => [tile.dataset.watchlistAccount, tile]));
    target.hidden = !items.length;
    for (const account of items) {
      const id = String(account.id);
      let tile = existing.get(id);
      if (!tile) {
        tile = node('button', undefined, 'positions-watchlist-tile');
        tile.type = 'button'; tile.dataset.watchlistAccount = id;
        tile.setAttribute('aria-controls', `positions-account-${id}`);
        tile.addEventListener('click', () => {
          setManagementOpen(true);
          const card = document.getElementById(`positions-account-${id}`);
          if (!card) return;
          card.open = true;
          syncWatchlistExpansion();
          card.querySelector('summary').focus({preventScroll: true});
          card.scrollIntoView({block: 'start'});
        });
        target.append(tile);
      }
      existing.delete(id);
      const readings = [account.positions, account.funding];
      const quality = readings.some(reading => reading?.state === 'error') ? 'Error'
        : readings.some(reading => reading?.state === 'stale') ? 'Stale'
        : readings.some(reading => reading?.state === 'delayed') ? 'Delayed'
        : readings.some(reading => !reading?.state || reading.state === 'pending') ? 'Pending'
        : readings.some(reading => !reading?.complete) ? 'Partial' : 'Current';
      const logo = tile.querySelector('.positions-tile-logo') || node('img', undefined, 'positions-tile-logo');
      const logoUrl = exchangeLogos[account.venue];
      if (logo.getAttribute('src') !== logoUrl) logo.src = logoUrl;
      logo.alt = ''; logo.width = 20; logo.height = 20;
      const name = node('span', undefined, 'positions-tile-line positions-tile-name');
      name.append(node('span', account.label, 'positions-tile-label'), node('span', labels[account.venue] || account.venue, 'positions-tile-venue'));
      const identity = node('span', undefined, 'positions-tile-line positions-tile-identity');
      const marker = node('span', quality === 'Current' ? '' : '!', `positions-tile-marker ${quality === 'Error' ? 'positions-error' : 'positions-warning'}`);
      marker.setAttribute('aria-hidden', 'true');
      identity.append(node('span', shortIdentity(account), 'positions-account-identity'), node('span', quality, `positions-tile-quality ${quality === 'Error' ? 'positions-error' : quality === 'Current' ? 'positions-muted' : 'positions-warning'}`), marker);
      const description = `${account.label} · ${labels[account.venue] || account.venue} · ${account.address} · Positions ${account.positions?.state || 'pending'}${account.positions?.complete ? '' : ' / incomplete'} · Funding ${account.funding?.state || 'pending'}${account.funding?.complete ? '' : ' / incomplete'}`;
      tile.title = description;
      tile.setAttribute('aria-label', `${description}; inspect account, portfolio totals unchanged`);
      tile.replaceChildren(logo, name, identity);
    }
    for (const tile of existing.values()) tile.remove();
    syncWatchlistExpansion();
  }
  function renderWatchlistSummary(items) {
    renderReadFreshness(items);
    $('positions-watchlist-count').textContent = String(items.length);
    $('positions-accounts-title').setAttribute('aria-label', `Watchlist, ${items.length} ${items.length === 1 ? 'account' : 'accounts'}`);
    renderWatchlistTiles(items);
    let attention = 0;
    let failed = false;
    for (const account of items) {
      if (account.positions?.state !== 'fresh' || !account.positions?.complete || account.funding?.state !== 'fresh' || !account.funding?.complete) attention += 1;
      failed ||= account.positions?.state === 'error' || account.funding?.state === 'error';
    }
    const health = $('positions-watchlist-health');
    const text = !items.length ? 'No accounts' : attention ? `${attention} ${attention === 1 ? 'needs' : 'need'} attention` : 'Readings current';
    const full = $('positions-watchlist-health-full');
    if (full.textContent !== text) full.textContent = text;
    $('positions-watchlist-health-short').textContent = attention ? `! ${attention}` : '';
    health.dataset.attention = String(attention);
    health.title = text;
    health.className = `positions-watchlist-health ${failed ? 'positions-error' : attention ? 'positions-warning' : 'positions-muted'}`;
    updateWatchlistMode();
  }
  function renderAccounts(data, force = false) {
    renderWatchlistSummary(data.accounts);
    const target = $('positions-account-list');
    // A background reading must not discard a label or token the user is entering.
    if (!force && target.contains(document.activeElement)) return;
    const opened = new Set([...target.querySelectorAll('details[data-account-id][open]')].map(el => el.dataset.accountId));
    target.replaceChildren();
    if (!data.accounts.length) target.append(emptyState('No accounts in this tab', 'Add a wallet address to begin.'));
    else {
      const titles = node('div', undefined, 'list-head positions-account-summary positions-account-list-head');
      titles.setAttribute('aria-hidden', 'true');
      for (const title of ['Account', 'Exchange', 'Address / index', 'Positions', 'Funding']) titles.append(node('span', title));
      target.append(titles);
    }
    for (const account of data.accounts) {
      const card = node('details', undefined, 'positions-account');
      card.dataset.accountId = String(account.id);
      card.id = `positions-account-${account.id}`;
      card.addEventListener('toggle', syncWatchlistExpansion);
      card.open = opened.has(String(account.id));
      card.dataset.state = account.positions?.state === 'error' || account.funding?.state === 'error' ? 'error' : !account.positions?.complete || !account.funding?.complete ? 'partial' : 'complete';
      const heading = node('summary', undefined, 'positions-account-heading');
      heading.append(accountHeading(account)); card.append(heading);
      const body = node('div', undefined, 'positions-account-body');
      body.append(node('p', account.address, 'positions-account-address'), status('Positions', account.positions), status('Settled funding', account.funding));
      const balances = node('div', undefined, 'positions-asset-line');
      balances.append(field('Balances, native', buckets(account.balances, 'Unavailable')), field('Equity, native', buckets(account.equity, 'Unavailable')));
      body.append(disclosure('Balances & equity · separate from exposure', balances));
      if (account.notes?.length) {
        const notes = node('div');
        for (const note of account.notes) notes.append(node('p', note, 'positions-warning'));
        body.append(disclosure(`Account notes (${account.notes.length})`, notes, 'disclosure positions-warning'));
      }
      const form = node('form', undefined, 'positions-account-actions');
      form.dataset.accountForm = String(account.id);
      const label = node('label', undefined, 'field');
      const input = node('input', undefined, 'input');
      input.name = 'label'; input.value = account.label; input.required = true; input.maxLength = 80;
      label.append(node('span', 'Label', 'field-label'), input); form.append(label);
      let token = null;
      if (account.venue === 'lighter') {
        body.append(node('p', `Read-only token: ${account.has_read_only_token ? 'present only in page memory; lost on refresh' : 'not present; re-enter after refresh if private funding requires it'}`, 'positions-muted'));
        const tokenLabel = node('label', undefined, 'field');
        tokenLabel.append(node('span', 'Read-only token (blank keeps current)', 'field-label'));
        token = node('input', undefined, 'input'); token.type = 'password'; token.name = 'read_only_token'; token.autocomplete = 'off'; token.spellcheck = false; token.maxLength = 2048; token.placeholder = 'ro:…';
        tokenLabel.append(token); form.append(tokenLabel);
        const link = node('a', 'Create a Lighter read-only token');
        link.href = 'https://app.lighter.xyz/read-only-tokens/'; link.target = '_blank'; link.rel = 'noopener noreferrer'; body.append(link);
      }
      const save = node('button', 'Save changes', 'btn btn-secondary'); save.type = 'submit'; form.append(save);
      form.addEventListener('submit', event => {
        event.preventDefault();
        const current = accounts.find(item => item.id === account.id);
        if (!current) return;
        try {
          const labelValue = validLabel(input.value);
          const replacement = token?.value.trim() ? validToken(token.value, current.address) : null;
          if (entrySubmission !== null) invalidateEntry('Editing an account cancelled discovery.');
          current.label = labelValue;
          const tokenChanged = replacement !== null && replacement !== current.readOnlyToken;
          if (tokenChanged) replaceToken(current, replacement);
          localError = null;
          input.blur();
          if (token) token.value = '';
          persistWatchlist(); maskScreen(tokenChanged ? 'Token replaced in page memory. Old funding history is cleared; fetching fresh readings.' : 'Tab label updated. Updating exact summaries.');
        } catch (error) { localError = error.message; notice(localError, 'error'); }
        finally { if (token) token.value = ''; }
      });
      if (account.venue === 'lighter' && account.has_read_only_token) {
        const clear = node('button', 'Clear token', 'btn btn-ghost'); clear.type = 'button'; clear.dataset.clearToken = String(account.id);
        clear.addEventListener('click', () => {
          const current = accounts.find(item => item.id === account.id);
          if (!current) return;
          if (entrySubmission !== null) invalidateEntry('Changing a token cancelled discovery.');
          if (token) token.value = '';
          replaceToken(current, ''); localError = null;
          maskScreen('Read-only token and funding history cleared from page memory. Public reads may work; private funding is incomplete until a token is re-entered.');
        });
        form.append(clear);
      }
      const edit = node('button', 'Edit account', 'btn btn-ghost'); edit.type = 'button';
      edit.addEventListener('click', () => {
        const current = accounts.find(item => item.id === account.id);
        if (!current) return;
        beginAccountEdit(current);
      });
      form.append(edit);
      const remove = node('button', 'Remove', 'btn btn-danger'); remove.type = 'button'; remove.dataset.deleteAccount = String(account.id);
      remove.addEventListener('click', () => {
        const index = accounts.findIndex(item => item.id === account.id);
        if (index < 0 || !window.confirm(`Remove ${accounts[index].label} from this tab? This does not modify the exchange account.`)) return;
        if (entrySubmission !== null) invalidateEntry('Removing an account cancelled discovery.');
        const current = accounts[index]; current.generation += 1; current.readOnlyToken = ''; clearFunding(current);
        if (activeRead?.account === current) activeRead.controller.abort();
        accounts.splice(index, 1);
        if (editingId === current.id || !accounts.length) resetAddForm();
        if (token) token.value = '';
        localError = null; persistWatchlist(); maskScreen(accounts.length ? 'Account removed. Previous totals are hidden while current summaries update.' : 'Tab watchlist is empty. Add an account to start fresh read-only requests.');
      });
      form.append(remove); body.append(form); card.append(body); target.append(card);
    }
    syncWatchlistExpansion();
  }
  function accountReading(title, reading) {
    const state = reading?.state || 'pending';
    let text = state;
    if (!reading?.complete && !['pending', 'error', 'stale'].includes(state)) text += ' · partial';
    if (reading?.checked_at && state !== 'fresh') text += ' · retained';
    const el = node('span', undefined, 'positions-account-reading');
    el.append(
      node('span', title, 'positions-account-reading-label'),
      badge(text, state === 'error' ? 'error' : state !== 'fresh' || !reading?.complete ? 'warning' : '')
    );
    return el;
  }
  function accountHeading(account) {
    const heading = node('span', undefined, 'positions-account-summary');
    const address = node('span', shortIdentity(account), 'positions-account-identity');
    address.setAttribute('aria-label', `${account.venue === 'lighter' ? 'Account index' : 'Address'} ${account.address}`);
    address.title = account.address;
    heading.append(
      node('span', account.label, 'positions-account-name'),
      node('span', labels[account.venue] || account.venue, 'positions-account-venue'),
      address,
      accountReading('Positions', account.positions),
      accountReading('Funding', account.funding)
    );
    return heading;
  }
  function clockFreshness() {
    if (!snapshot) return;
    let hasChanged = false;
    const currentAccounts = localAccounts().accounts;
    for (const account of snapshot.accounts) {
      let accountChanged = false;
      const current = currentAccounts.find(item => item.id === account.id);
      for (const kind of ['positions', 'funding']) {
        const data = account[kind];
        // Downgrade retained totals immediately, but never upgrade old totals with
        // the timestamp or success of a newer reading before exact aggregation.
        if (current?.[kind].error && data.state !== 'error') {
          data.state = 'error'; data.error = current[kind].error; data.complete = false;
          data.attempted_at = current[kind].attempted_at;
          hasChanged = true; accountChanged = true;
        }
        if (kind === 'funding' && data.complete && !current?.funding.complete) {
          data.complete = false; data.catching_up = current?.funding.catching_up || false;
          hasChanged = true; accountChanged = true;
        }
        if (!data?.checked_at || ['pending', 'error'].includes(data.state)) continue;
        const elapsed = Math.max(0, (Date.now() - Date.parse(data.checked_at)) / 1000);
        const [fresh, stale] = kind === 'positions' ? [120, 300] : [600, 900];
        const state = elapsed <= fresh ? 'fresh' : elapsed <= stale ? 'delayed' : 'stale';
        if (state !== data.state) {
          data.state = state; data.complete = data.complete && state !== 'stale';
          hasChanged = true; accountChanged = true;
        }
      }
      if (accountChanged && current) updateCardStatus(current);
    }
    const fxExpired = snapshot.fx?.checked_at && Date.now() - Date.parse(snapshot.fx.checked_at) > 300000;
    if (fxExpired && !snapshot.fx.expired) {
      snapshot.fx.expired = true; snapshot.fx.rates = {USD: '1'}; hasChanged = true;
      if (snapshot.totals.funding.some(value => value.currency !== 'USD')) snapshot.totals.funding_usd = null;
    }
    if (!hasChanged) return;
    snapshot.totals.positions_complete = snapshot.totals.positions_complete && snapshot.accounts.every(account => account.positions.complete);
    snapshot.totals.funding_complete = snapshot.totals.funding_complete && snapshot.accounts.every(account => account.funding.complete);
    if (!snapshot.totals.positions_complete) snapshot.totals.net_delta_usd = null;
    if (!snapshot.totals.funding_complete) snapshot.totals.funding_usd = null;
    for (const asset of snapshot.assets) {
      asset.partial = asset.partial || !snapshot.totals.positions_complete || !snapshot.totals.funding_complete;
      let priceUnavailable = false;
      for (const leg of asset.legs) {
        leg.state = snapshot.accounts.find(account => account.id === leg.account_id)?.positions.state || leg.state;
        priceUnavailable = priceUnavailable || ['stale', 'error', 'pending'].includes(leg.state) || (fxExpired && leg.quote_currency && leg.quote_currency !== 'USD');
      }
      if (priceUnavailable) { asset.reference_price_usd = null; asset.net_delta_usd = null; snapshot.totals.net_delta_usd = null; }
      if (!snapshot.totals.funding_complete || (fxExpired && asset.funding.some(value => value.currency !== 'USD'))) asset.funding_usd = null;
    }
    renderTotals(snapshot); renderAssets(snapshot); changed();
  }
  function updateCardStatus(account) {
    renderWatchlistSummary(localAccounts().accounts);
    const card = document.querySelector(`[data-account-id="${account.id}"]`);
    if (!card) return;
    card.dataset.state = account.positions.state === 'error' || account.funding.state === 'error' ? 'error' : !account.positions.complete || !account.funding.complete ? 'partial' : 'complete';
    card.querySelector('summary').replaceChildren(accountHeading(account));
    for (const kind of ['positions', 'funding']) card.querySelector(`[data-position-status-kind="${kind}"]`)?.replaceWith(status(kind === 'positions' ? 'Positions' : 'Settled funding', account[kind]));
  }
  function cursorKey(value) {
    if (Array.isArray(value)) return `[${value.map(cursorKey).join(',')}]`;
    if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${cursorKey(value[key])}`).join(',')}}`;
    return JSON.stringify(value);
  }
  function validTime(value) { return typeof value === 'string' && Number.isFinite(Date.parse(value)); }
  async function readAccount(read) {
    const {account, kind, controller} = read;
    const generation = account.generation;
    const fundingGeneration = account.fundingGeneration;
    const hours = Number($('positions-period').value);
    const previousScan = kind === 'funding' ? account.fundingScan : null;
    const current = () => accounts.includes(account) && generation === account.generation && (kind !== 'funding' || fundingGeneration === account.fundingGeneration && hours === Number($('positions-period').value));
    account[`${kind}_attempted_at`] = new Date().toISOString();
    changed();
    try {
      const body = {venue: account.venue, address: account.address, kind,
        ...(kind === 'funding' ? {hours, scan: previousScan, ...(account.readOnlyToken ? {read_only_token: account.readOnlyToken} : {})} : {})};
      const data = await api('/api/positions/read', body, controller);
      if (!current()) return;
      if (!validTime(data.attempted_at)) throw new Error('Venue response lacks a valid attempt timestamp. Last good readings are retained in memory.');
      if (kind === 'positions') {
        if (!validTime(data.checked_at) || !data.snapshot || !['positions', 'balances', 'equity', 'notes'].every(key => Array.isArray(data.snapshot[key]))) throw new Error('Position response is incomplete. Last good positions are retained in memory.');
        account.snapshot = data.snapshot; account.positions_checked_at = data.checked_at; account.positions_attempted_at = data.attempted_at;
        account.positions_error = null; account.positionsFailures = 0; account.nextPositionsAt = Date.now() + 60000;
      } else {
        const scan = data.scan;
        if (!Array.isArray(data.events) || typeof data.complete !== 'boolean' || !scan || !validTime(scan.since) || !validTime(scan.until) || !validTime(scan.coverage_since) || (data.checked_at !== null && !validTime(data.checked_at)) || Date.parse(scan.since) >= Date.parse(scan.until) || Date.parse(scan.until) - Date.parse(scan.since) !== hours * 3600000 || (previousScan && (scan.since !== previousScan.since || scan.until !== previousScan.until))) throw new Error('Funding scan bounds changed or the response is incomplete. Retaining only known history in memory.');
        const cursor = scan.cursor;
        const key = cursorKey(cursor);
        if (!data.complete && (!cursor || typeof cursor !== 'object' || Array.isArray(cursor) || account.cursors.has(key) || previousScan && key === cursorKey(previousScan.cursor))) throw new Error('Funding scan did not advance or repeated a page. Known amounts are incomplete.');
        if (data.complete && (cursor !== null || !validTime(data.checked_at))) throw new Error('Funding scan completion is inconsistent. Known amounts are incomplete.');
        const page = new Map();
        for (const event of data.events) {
          if (!event || typeof event.event_id !== 'string' || !event.event_id || !validTime(event.ts) || !['symbol', 'currency', 'scope', 'amount'].every(field => typeof event[field] === 'string') || !/^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(event.amount) || Date.parse(event.ts) < Date.parse(scan.since) || Date.parse(event.ts) > Date.parse(scan.until)) throw new Error('Funding page is incomplete or outside the fixed scan bounds. Last good history is retained in memory.');
          page.set(event.event_id, {event_id: event.event_id, ts: event.ts, symbol: event.symbol, amount: event.amount, currency: event.currency, scope: event.scope});
        }
        let count = accounts.reduce((total, item) => total + item.events.size, 0);
        for (const id of page.keys()) if (!account.events.has(id)) count += 1;
        for (const [id, event] of page) account.events.set(id, event);
        if (count > 50000) {
          account.fundingScan = {since: scan.since, until: scan.until, cursor, coverage_since: scan.coverage_since, coverage_note: scan.coverage_note || null};
          account.funding_since = scan.coverage_since; account.funding_coverage_note = scan.coverage_note || null;
          const error = new Error('Funding history exceeds the 50,000-event summary limit. All received rows remain in memory, but summaries are unavailable and history is incomplete; select a shorter period or remove accounts.');
          error.blocked = true;
          throw error;
        }
        if (!data.complete) account.cursors.add(key);
        account.fundingScan = data.complete ? null : {since: scan.since, until: scan.until, cursor, coverage_since: scan.coverage_since, coverage_note: scan.coverage_note || null};
        account.fundingComplete = data.complete;
        account.funding_since = scan.coverage_since; account.funding_coverage_note = scan.coverage_note || null;
        account.funding_checked_at = data.checked_at || account.funding_checked_at; account.funding_attempted_at = data.attempted_at;
        account.funding_error = null; account.fundingFailures = 0;
        account.nextFundingAt = Date.now() + (data.complete ? 300000 : 3000);
        if (data.complete) {
          account.cursors.clear();
          for (const [id, event] of account.events) if (Date.parse(event.ts) < Date.parse(scan.since)) account.events.delete(id);
        }
      }
    } catch (error) {
      if (!current()) return;
      account[`${kind}_error`] = error.message;
      const failures = ++account[`${kind}Failures`];
      const delay = Math.min(300000, (kind === 'positions' || previousScan ? 60000 : 300000) * 2 ** Math.min(failures - 1, 3));
      account[kind === 'positions' ? 'nextPositionsAt' : 'nextFundingAt'] = error.blocked ? Infinity : Date.now() + delay;
      if (kind === 'funding') account.fundingComplete = false;
    } finally {
      if (current()) {
        changed();
        for (const accountState of localAccounts().accounts) updateCardStatus(accountState);
      }
      if (activeRead === read) activeRead = null;
      nextReadAt = Date.now() + 1500;
      $('positions-refresh').disabled = resolving;
    }
  }
  function pumpReads() {
    if (activeRead || resolving || !accounts.length || Date.now() < nextReadAt) return;
    let selected = null;
    for (const account of accounts) for (const kind of ['positions', 'funding']) {
      const due = account[kind === 'positions' ? 'nextPositionsAt' : 'nextFundingAt'];
      if (due <= Date.now() && (!selected || due < selected.due)) selected = {account, kind, due};
    }
    if (!selected) return;
    activeRead = {account: selected.account, kind: selected.kind, controller: new AbortController()};
    $('positions-refresh').disabled = true;
    activeRead.promise = readAccount(activeRead);
  }
  async function loadFx() {
    if (fxBusy || !accounts.length || Date.now() < fxNextAt) return;
    fxBusy = true;
    try {
      const result = await api('/api/positions/fx');
      if (!validTime(result.ts) || !result.rates || typeof result.rates !== 'object' || typeof result.source !== 'string') throw new Error('Public FX is unavailable; native amounts are unchanged.');
      fx = {ts: result.ts, rates: result.rates, source: result.source}; fxError = null;
    } catch (_) { fxError = 'FX read unavailable; retained rates expire after five minutes. USD estimates may be unavailable.'; }
    finally { fxBusy = false; fxNextAt = Date.now() + 300000; changed(); }
  }
  async function aggregateMemory() {
    if (!aggregateDirty || aggregateBusy || !accounts.length || Date.now() < aggregateNextAt) return;
    aggregateBusy = true; aggregateDirty = false;
    const version = dataVersion;
    const visible = visibilityVersion;
    const hours = Number($('positions-period').value);
    const controller = new AbortController(); aggregateController = controller;
    try {
      const data = await api('/api/positions/aggregate', {hours,
        accounts: accounts.map(account => ({id: account.id, venue: account.venue, address: account.address, label: account.label, created_at: account.created_at,
          snapshot: account.snapshot, has_read_only_token: Boolean(account.readOnlyToken),
          positions_checked_at: account.positions_checked_at, positions_attempted_at: account.positions_attempted_at, positions_error: account.positions_error,
          funding_checked_at: account.funding_checked_at, funding_attempted_at: account.funding_attempted_at, funding_error: account.funding_error,
          funding_since: account.funding_since, funding_cursor: account.fundingScan, funding_coverage_note: account.funding_coverage_note})),
        events: accounts.flatMap(account => [...account.events.values()].map(event => ({account_id: account.id, ...event}))), fx}, controller);
      if (visible !== visibilityVersion || version !== dataVersion) return;
      if (data.hours !== hours || !Array.isArray(data.accounts) || !Array.isArray(data.assets) || !data.totals || data.accounts.length !== accounts.length || data.accounts.some(item => !accounts.some(account => account.id === item.id))) throw new Error('Exact summary response is incomplete. Current watchlist totals remain unknown.');
      snapshot = data; snapshotVersion = visible; screenOffline = false;
      aggregateFailures = 0; aggregateNextAt = 0;
      renderTotals(data); renderAssets(data); renderAccounts(data);
      const complete = data.totals.positions_complete && data.totals.funding_complete;
      notice(localError || (complete ? 'Readings updated. Manage shows account timestamps and coverage.' : 'Partial or retained readings — use Manage to inspect account status before treating totals as current.'), localError ? 'error' : complete ? '' : 'warning');
    } catch (error) {
      if (visible !== visibilityVersion) return;
      screenOffline = true;
      aggregateFailures += 1;
      aggregateNextAt = Date.now() + Math.min(300000, 30000 * 2 ** Math.min(aggregateFailures - 1, 4));
      aggregateDirty = true;
      const retained = snapshot && snapshotVersion === visible;
      notice(`${error.message}${retained ? ' Retaining only the same watchlist and period’s last screen in memory; it is not confirmed current.' : ' Exact totals remain unknown; previous watchlist or period amounts are hidden.'}`, 'error');
      if (retained) markRetainedScreen();
      renderAccounts(localAccounts());
    } finally {
      aggregateBusy = false;
      if (aggregateController === controller) aggregateController = null;
    }
  }
  function selectedVenues() {
    return exchangeInputs.filter(input => input.checked && !input.disabled).map(input => input.value);
  }
  function entryAddressType(raw) {
    if (/^0x[0-9a-fA-F]{40}$/.test(raw)) return 'evm';
    if (/^[0-9]{1,15}$/.test(raw) && Number(raw) <= 2 ** 48 - 1) return 'index';
    try { canonicalAddress('bulk', raw); return 'solana'; } catch (_) { return 'unknown'; }
  }
  function entryMessage(text, state = '') {
    const result = $('positions-entry-result');
    result.textContent = text;
    result.dataset.state = state;
    result.className = `positions-wide ${state === 'error' ? 'positions-error' : state === 'warning' ? 'positions-warning' : 'positions-muted'}`;
  }
  function renderDiscovery(operation, summary = '') {
    const failures = operation.reports.filter(report => report.state === 'error').length;
    const warnings = operation.reports.some(report => report.notes.size || report.state === 'cancelled');
    if (!summary) {
      summary = operation.finished
        ? `Added ${operation.added} ${operation.added === 1 ? 'account' : 'accounts'}.${failures ? ` ${failures} ${failures === 1 ? 'exchange needs' : 'exchanges need'} attention; this watchlist may omit owned accounts.` : ''}${warnings ? ' Review the coverage notes below.' : ''}`
        : `Discovering accounts on selected exchanges… ${operation.added} added so far.`;
    }
    entryMessage('', failures ? 'error' : warnings ? 'warning' : '');
    const result = $('positions-entry-result');
    result.append(node('p', summary));
    const list = node('ul', undefined, 'positions-discovery-results');
    for (const report of operation.reports) {
      const item = node('li');
      item.dataset.discoveryVenue = report.venue;
      item.dataset.state = report.state;
      item.append(node('strong', `${labels[report.venue]}: `), node('span', report.text));
      item.className = report.state === 'error' ? 'positions-error' : report.state === 'cancelled' ? 'positions-warning' : 'positions-muted';
      for (const note of report.notes) item.append(node('p', note, 'positions-warning'));
      list.append(item);
    }
    result.append(list);
  }
  function invalidateEntry(reason = 'The form changed. Submit again to discover accounts for the current input.') {
    entryVersion += 1;
    const operation = entrySubmission;
    entrySubmission = null;
    operation?.controller?.abort();
    resolving = false;
    if (operation?.reports.length) {
      for (const report of operation.reports) {
        if (report.state !== 'pending' && report.state !== 'working') continue;
        report.state = 'cancelled';
        report.text = `Discovery cancelled${report.found ? ` after finding ${report.found} accounts` : ''}; no accounts added for this exchange.`;
      }
      operation.finished = true;
      const summary = `${reason} ${operation.added ? `${operation.added} accounts had already been added; unfinished exchanges added nothing.` : 'No accounts were added.'}`;
      renderDiscovery(operation, summary);
      notice(summary, 'warning');
    } else entryMessage('');
    localError = null;
    tokenFields();
    $('positions-refresh').disabled = Boolean(activeRead);
  }
  function discoveryWallet(venue, raw) {
    if (venue === 'lighter' && /^0x[0-9a-fA-F]{40}$/.test(raw)) return raw.toLowerCase();
    return canonicalAddress(venue, raw);
  }
  async function discoverOwnedAccounts(operation, report, wallet, unchanged) {
    const found = new Map();
    const seenCursors = new Set();
    const existing = new Set(accounts.filter(account => account.venue === report.venue).map(account => account.address));
    const available = 32 - accounts.length;
    let missing = 0;
    let cursor = null;
    while (unchanged()) {
      const controller = new AbortController();
      operation.controller = controller;
      const data = await api('/api/positions/accounts', {venue: report.venue, wallet, ...(cursor === null ? {} : {cursor})}, controller);
      if (!unchanged()) return [];
      operation.controller = null;
      if (data.venue !== report.venue || typeof data.wallet !== 'string' || discoveryWallet(report.venue, data.wallet) !== wallet || !Array.isArray(data.accounts) ||
          !Object.hasOwn(data, 'next_cursor') || data.next_cursor !== null && (typeof data.next_cursor !== 'string' || !data.next_cursor)) {
        throw new Error('The account discovery response is incomplete.');
      }
      if (data.note !== undefined && typeof data.note !== 'string') throw new Error('The account discovery coverage note is incomplete.');
      if (data.note?.trim()) report.notes.add(data.note.trim());
      report.pages += 1;
      for (const row of data.accounts) {
        if (!row || typeof row.address !== 'string' || row.name !== undefined && row.name !== null && typeof row.name !== 'string') throw new Error('An account discovery row is incomplete.');
        const address = canonicalAddress(report.venue, row.address);
        if (!found.has(address)) {
          found.set(address, {address, name: row.name || ''});
          if (!existing.has(address)) missing += 1;
          report.found = found.size;
        }
      }
      report.found = found.size;
      report.complete = data.next_cursor === null;
      report.text = `Found ${found.size} distinct ${found.size === 1 ? 'account' : 'accounts'} across ${report.pages} ${report.pages === 1 ? 'page' : 'pages'}${report.complete ? '; checking additions…' : '; following the next page automatically…'}`;
      renderDiscovery(operation);
      if (missing > available) {
        throw new Error(`Found ${missing}${report.complete ? '' : ' or more'} new accounts, but only ${available} of this tab’s 32 slots are available.${report.complete ? '' : ' Discovery stopped before all pages were read.'} Remove accounts or select fewer exchanges; this exchange was not partially added.`);
      }
      if (report.complete) return [...found.values()];
      if (seenCursors.has(data.next_cursor)) throw new Error('The exchange repeated a discovery cursor; remaining ownership is unknown.');
      if (report.pages >= 100) throw new Error('Discovery stopped at the 100-page safety limit before completion; remaining ownership is unknown.');
      seenCursors.add(data.next_cursor);
      cursor = data.next_cursor;
    }
    return [];
  }
  function discoveredLabel(shared, venue, wallet, row, count) {
    if (count === 1 || row.address === wallet) return shared;
    const identity = shortIdentity({venue, address: row.address});
    const name = row.name.replace(/[\x00-\x1f\x7f]/g, ' ').replace(/\s+/g, ' ').trim();
    const suffix = name ? `${[...name].slice(0, 32).join('')} · ${identity}` : venue === 'lighter' ? `Account ${row.address}` : `Subaccount ${identity}`;
    return `${[...shared].slice(0, 77 - [...suffix].length).join('')} · ${suffix}`;
  }
  function tokenFields() {
    const wallet = $('positions-address').value.trim();
    const type = entryAddressType(wallet);
    const compatible = type === 'evm' ? ['hyperliquid', 'risex', 'lighter'] : type === 'solana' ? ['bulk'] : type === 'index' ? ['lighter'] : Object.keys(labels);
    for (const input of exchangeInputs) {
      input.disabled = !compatible.includes(input.value);
      input.checked = !input.disabled && chosenVenues.has(input.value);
    }
    const lighter = selectedVenues().includes('lighter');
    const help = type === 'evm' ? 'EVM wallet: Hyperliquid, RISEx and Lighter. BULK needs a Solana address.' : type === 'solana' ? 'Solana address: select BULK.' : type === 'index' ? 'Known account index: Lighter only. Enter its wallet to discover sibling accounts.' : 'EVM wallets use Hyperliquid, RISEx and Lighter; Solana keys use BULK; a known index uses Lighter.';
    $('positions-exchange-help').textContent = editingId ? `Editing one account: choose one exchange. ${help}` : help;
    $('positions-address-label').textContent = editingId ? 'Address, public key or Lighter index' : 'Wallet, public key or Lighter index';
    $('positions-address-help').textContent = editingId
      ? 'Changes this one account only. Use Add to discover a wallet’s accounts.'
      : 'Discovers owned subaccounts; up to 32 accounts per tab.';
    $('positions-token-fields').hidden = !lighter;
    $('positions-address').inputMode = 'text';
    $('positions-address').removeAttribute('pattern');
    $('positions-add-submit').setAttribute('aria-busy', String(entrySubmission !== null));
    $('positions-add-submit').disabled = entrySubmission !== null;
    $('positions-edit-cancel').hidden = !editingId && entrySubmission === null;
    $('positions-edit-cancel').textContent = editingId ? 'Cancel edit' : 'Cancel discovery';
    $('positions-token').disabled = !lighter;
    $('positions-token').placeholder = editingId ? 'ro:… (blank preserves for unchanged account)' : 'ro:…';
    if (!lighter) $('positions-token').value = '';
  }
  function resetAddForm() {
    invalidateEntry('Discovery cancelled.');
    editingId = null;
    chosenVenues = new Set(['hyperliquid', 'risex']);
    $('positions-add-form').reset();
    $('positions-token').value = '';
    $('positions-add-title').textContent = 'Add read-only accounts';
    $('positions-add-submit').textContent = 'Discover accounts';
    entryMessage('');
    tokenFields();
    $('positions-add').open = !accounts.length;
  }
  function beginAccountEdit(account) {
    const current = accounts.find(item => item.id === account.id);
    if (!current) return;
    resetAddForm(); editingId = current.id; chosenVenues = new Set([current.venue]);
    $('positions-address').value = current.address; $('positions-label').value = current.label;
    $('positions-add-title').textContent = 'Edit this tab account';
    $('positions-add-submit').textContent = 'Save account';
    $('positions-add').open = true;
    updateWatchlistMode();
    tokenFields(); $('positions-address').focus();
  }
  function refreshReads() {
    localError = null;
    if (!accounts.length) { notice('Tab watchlist is empty. Add an account to start fresh read-only requests.'); return; }
    for (const account of accounts) { account.nextPositionsAt = 0; account.nextFundingAt = 0; }
    fxNextAt = 0; aggregateNextAt = 0;
    changed();
    notice('Requesting fresh venue readings, one account operation at a time. Existing funding scans keep their fixed bounds.', 'warning');
    tick();
  }
  function tick() { pumpReads(); loadFx(); aggregateMemory(); }
  for (const input of exchangeInputs) input.addEventListener('change', () => {
    if (editingId && input.checked) chosenVenues = new Set([input.value]);
    else if (input.checked) chosenVenues.add(input.value);
    else chosenVenues.delete(input.value);
    invalidateEntry();
  });
  $('positions-address').addEventListener('input', () => invalidateEntry());
  $('positions-label').addEventListener('input', () => invalidateEntry());
  $('positions-token').addEventListener('input', () => invalidateEntry());
  $('positions-edit-cancel').addEventListener('click', () => {
    if (editingId) resetAddForm();
    else {
      invalidateEntry('Discovery cancelled.');
      $('positions-token').value = '';
      $('positions-address').focus();
    }
  });
  function setManagementOpen(open) {
    $('positions-management').hidden = !open;
    $('positions-manage').setAttribute('aria-expanded', String(open));
    $('positions-manage').textContent = open ? 'Done' : 'Manage';
    syncWatchlistExpansion();
    updateWatchlistMode();
  }
  $('positions-manage').addEventListener('click', () => {
    const open = $('positions-management').hidden;
    setManagementOpen(open);
    if (open) $('positions-management').scrollIntoView({block: 'start'});
  });
  $('positions-open-add').addEventListener('click', () => {
    if (editingId) resetAddForm();
    $('positions-add').open = true;
    updateWatchlistMode();
    $('positions-address').focus();
  });
  $('positions-add').addEventListener('toggle', () => {
    const open = $('positions-add').open;
    $('positions-open-add').setAttribute('aria-expanded', String(open));
    updateWatchlistMode();
    if (!open && entrySubmission !== null) invalidateEntry('Closing the form cancelled discovery.');
    if (!open && $('positions-add').contains(document.activeElement)) $('positions-open-add').focus();
  });
  $('positions-period').addEventListener('change', () => {
    localError = null;
    for (const account of accounts) clearFunding(account);
    maskScreen(accounts.length ? 'Loading selected funding period — all previous-period amounts are hidden; positions remain in page memory.' : 'Tab watchlist is empty. Add an account to start fresh read-only requests.');
    tick();
  });
  $('positions-refresh').addEventListener('click', refreshReads);
  $('positions-clear').addEventListener('click', () => {
    if (accounts.length && !window.confirm('Clear this tab’s watchlist, tokens and financial history? This does not modify exchange accounts.')) return;
    for (const account of accounts) { account.generation += 1; account.readOnlyToken = ''; clearFunding(account); }
    activeRead?.controller.abort();
    accounts.length = 0; fx = null; fxError = null; fxNextAt = 0; localError = null;
    resetAddForm(); persistWatchlist(); maskScreen('Watchlist, tokens and financial history cleared from this tab. No accounts are being polled.');
  });
  $('positions-add-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (entrySubmission !== null) return;
    const version = entryVersion;
    const editId = editingId;
    const venues = selectedVenues();
    const rawAddress = $('positions-address').value.trim();
    const rawLabel = $('positions-label').value.trim();
    const rawToken = $('positions-token').value.trim();
    const operation = {controller: null, reports: [], added: 0, finished: false};
    const unchanged = () => entrySubmission === operation && version === entryVersion && editId === editingId &&
      rawAddress === $('positions-address').value.trim() && rawLabel === $('positions-label').value.trim() &&
      rawToken === $('positions-token').value.trim() && venues.join(',') === selectedVenues().join(',');
    entrySubmission = operation;
    resolving = !editId;
    tokenFields();
    $('positions-refresh').disabled = resolving || Boolean(activeRead);
    try {
      if (!venues.length) throw new Error('Choose at least one compatible exchange. A Solana public key uses BULK; a known numeric index uses Lighter.');
      const label = validLabel(rawLabel);
      if (editId) {
        if (venues.length !== 1) throw new Error('Editing changes one existing account. Choose exactly one exchange.');
        const current = accounts.find(account => account.id === editId);
        if (!current) throw new Error('This account was removed. Cancel editing and add it again.');
        const venue = venues[0];
        const address = canonicalAddress(venue, rawAddress);
        if (accounts.some(account => account !== current && account.venue === venue && account.address === address)) throw new Error('This venue and account address are already in this tab’s watchlist. Edit the existing account instead.');
        const token = rawToken && venue === 'lighter' ? validToken(rawToken, address) : '';
        const identityChanged = current.venue !== venue || current.address !== address;
        if (identityChanged) {
          current.generation += 1;
          if (activeRead?.account === current) activeRead.controller.abort();
          current.venue = venue; current.address = address; current.readOnlyToken = token;
          current.snapshot = null; current.positions_checked_at = null; current.positions_attempted_at = null; current.positions_error = null;
          current.positionsFailures = 0; current.nextPositionsAt = 0; clearFunding(current);
        } else if (token && token !== current.readOnlyToken) replaceToken(current, token);
        current.label = label;
        const message = 'Tab account updated. Fetching fresh readings for any changed address or token.';
        localError = null; resetAddForm(); persistWatchlist();
        maskScreen(message); entryMessage(message);
        tick();
        return;
      }
      operation.reports = venues.map(venue => ({venue, state: 'pending', text: 'Waiting to discover accounts…', notes: new Set(), found: 0, pages: 0, complete: false}));
      renderDiscovery(operation, activeRead ? 'Waiting for the current account read, then discovering selected exchanges…' : '');
      if (activeRead) await activeRead.promise;
      if (!unchanged()) return;
      for (const report of operation.reports) {
        if (!unchanged()) return;
        report.state = 'working';
        report.text = 'Discovering owned accounts…';
        renderDiscovery(operation);
        try {
          const wallet = discoveryWallet(report.venue, rawAddress);
          const token = report.venue === 'lighter' && rawToken ? validToken(rawToken) : '';
          const tokenParts = token ? token.split(':') : null;
          const tokenIndex = tokenParts ? canonicalAddress('lighter', tokenParts[1]) : null;
          const rows = await discoverOwnedAccounts(operation, report, wallet, unchanged);
          if (!unchanged()) return;
          if (tokenParts?.[2] === 'single' && rows.length && !rows.some(row => row.address === tokenIndex)) throw new Error(`This single-account token is for Lighter #${tokenIndex}, which was not returned for this wallet or index. No token was assigned.`);
          const missing = rows.filter(row => !accounts.some(account => account.venue === report.venue && account.address === row.address));
          const skipped = rows.length - missing.length;
          if (accounts.length + missing.length > 32) throw new Error(`This exchange needs ${missing.length} new accounts, but only ${32 - accounts.length} of this tab’s 32 slots are available. Remove accounts or select fewer exchanges; this exchange was not partially added.`);
          const createdAt = new Date().toISOString();
          const additions = missing.map(row => {
            const account = memoryAccount({id: crypto.randomUUID(), venue: report.venue, address: row.address,
              label: validLabel(discoveredLabel(label, report.venue, wallet, row, rows.length)), created_at: createdAt});
            if (token && (tokenParts[2] === 'all' || row.address === tokenIndex)) account.readOnlyToken = validToken(token, row.address);
            return account;
          });
          const publicOnly = tokenParts?.[2] === 'single' ? additions.filter(account => !account.readOnlyToken).length : 0;
          if (publicOnly) report.notes.add(`The single-account token applies only to Lighter #${tokenIndex}. ${publicOnly} other newly added ${publicOnly === 1 ? 'account has' : 'accounts have'} no token; private funding may be incomplete. Tokens can be set separately in Manage.`);
          accounts.push(...additions);
          operation.added += additions.length;
          report.state = 'success';
          report.text = rows.length
            ? `${additions.length ? `Added ${additions.length} ${additions.length === 1 ? 'account' : 'accounts'}.` : 'No new accounts.'}${skipped ? ` Skipped ${skipped} already watched; labels and tokens unchanged.` : ''}`
            : 'No owned accounts returned for this wallet; no accounts added.';
          if (additions.length) {
            persistWatchlist();
            maskScreen(`Added ${operation.added} discovered ${operation.added === 1 ? 'account' : 'accounts'}. Discovery is still running; do not treat this watchlist as complete yet.`);
          }
        } catch (error) {
          if (!unchanged()) return;
          operation.controller = null;
          report.state = 'error';
          report.text = `${report.complete ? 'No accounts added for this exchange.' : `Discovery incomplete${report.pages ? ` after ${report.pages} ${report.pages === 1 ? 'page' : 'pages'} (${report.found} distinct accounts found)` : ''}; no accounts added for this exchange.`} ${error.message}`;
        }
        renderDiscovery(operation);
      }
      if (!unchanged()) return;
      operation.finished = true;
      renderDiscovery(operation);
      const failed = operation.reports.filter(report => report.state === 'error');
      const limited = operation.reports.some(report => report.notes.size);
      localError = failed.length ? `Wallet discovery needs attention on ${failed.map(report => labels[report.venue]).join(', ')}. This watchlist may omit owned accounts; review the add results.` : null;
      notice(localError || `Account discovery finished. ${operation.added} ${operation.added === 1 ? 'account' : 'accounts'} added.${limited ? ' Coverage notes apply; review the add results.' : ''}${operation.added ? ' Fetching fresh readings…' : ' Existing labels and tokens are unchanged.'}`, failed.length ? 'error' : limited ? 'warning' : '');
    } catch (error) {
      if (!unchanged()) return;
      localError = `${editId ? 'No changes were saved.' : 'No accounts were added.'} ${error.message}`;
      entryMessage(localError, 'error'); notice(localError, 'error');
    } finally {
      if (entrySubmission === operation) {
        entrySubmission = null; resolving = false;
        $('positions-token').value = '';
        tokenFields();
        $('positions-refresh').disabled = Boolean(activeRead);
        nextReadAt = Date.now() + 1500;
        tick();
      }
    }
  });
  window.addEventListener('pagehide', () => {
    invalidateEntry('Navigation cancelled discovery.');
    $('positions-token').value = '';
  });
  restoreWatchlist(); tokenFields();
  new ResizeObserver(updateWatchlistGeometry).observe($('positions-watchlist-sticky'));
  window.addEventListener('scroll', () => queueWatchlistMode(), {passive: true});
  window.addEventListener('resize', () => queueWatchlistMode(true));
  $('positions-watchlist-sticky').addEventListener('focusin', () => queueWatchlistMode());
  $('positions-watchlist-sticky').addEventListener('focusout', () => queueWatchlistMode());
  $('positions-add').open = !accounts.length;
  maskScreen(accounts.length ? 'Tab watchlist restored. Fetching fresh venue readings; tokens and financial history were not restored.' : 'Anonymous tab watchlist ready. Add an account to request fresh read-only venue data.');
  setInterval(() => {
    clockFreshness();
    renderReadFreshness(localAccounts().accounts);
    document.querySelectorAll('[data-positions-age]').forEach(el => { el.textContent = age(el.dataset.positionsAge); });
    tick();
  }, 1000);
  tick();
})();
