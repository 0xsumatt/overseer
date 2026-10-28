(() => {
// VENUE_COLORS / VENUE_ORDER / colorFor come from venues.js — one definition
// for every page, rather than three hand-synced copies that drifted.

const { series: SERIES } = JSON.parse(document.getElementById('dashboard-config').textContent);

const MAX_CHARTS = 4;
const dashboardFilters = Overseer.filterState('charts');
const dashboardState = Overseer.pageState();
let market = dashboardFilters.get('market') === 'spot' ? 'spot' : 'perp';
let dashboardView = dashboardFilters.get('view') === 'charts' ? 'charts' : 'overview';
let chartMetric = ['spread', 'volatility'].includes(dashboardFilters.get('metric'))
  ? dashboardFilters.get('metric') : 'price';
const SHARED_ASSETS_KEY = 'assets';
const SHARED_EXCHANGES_KEY = 'exchanges';
const EXCHANGE_STATE_VERSION_KEY = 'exchange-state-version';
const EXCHANGE_STATE_VERSION = '2';
if (dashboardFilters.get(EXCHANGE_STATE_VERSION_KEY) !== EXCHANGE_STATE_VERSION) {
  // v1 saved only the exchanges visible in the active market. Applying "all"
  // on Spot therefore hid every perp-only venue after switching back.
  dashboardFilters.set(SHARED_EXCHANGES_KEY, null);
  dashboardFilters.set(EXCHANGE_STATE_VERSION_KEY, EXCHANGE_STATE_VERSION);
}
// Discard the removed per-chart controls without resetting the global choice.
const obsoleteExchangeKeys = new Set([
  'exchange-override-overview',
  ...SERIES.map(series => `exchange-override-${series.market_type}-${series.asset}`),
]);
obsoleteExchangeKeys.forEach(key => {
  if (dashboardFilters.get(key) !== null) dashboardFilters.set(key, null);
});
if (dashboardFilters.get(SHARED_ASSETS_KEY) === null) {
  const legacyAssets = dashboardFilters.get(`assets-${market}`) ??
    dashboardFilters.get(`assets-${market === 'perp' ? 'spot' : 'perp'}`);
  if (legacyAssets !== null) dashboardFilters.set(SHARED_ASSETS_KEY, legacyAssets);
}
dashboardFilters.set('assets-perp', null);
dashboardFilters.set('assets-spot', null);
let stagedAssets = new Set();
const multiCards = new Map();
const discoveredExchanges = { spot: new Set(), perp: new Set() };
let volumeOiChart = null;
let volumeOiReadout = null;
let volumeOiRequest = 0;
const volumeOiSeries = new Map();
let volatilityHours = Number(dashboardFilters.get('volatility-hours'));
if (![1, 6, 24].includes(volatilityHours)) volatilityHours = 24;
const chartFilters = document.getElementById('chart-filters');

// Axis formats: one precision per axis.
const SPREAD_FORMAT = Object.freeze({
  type: 'custom', minMove: 0.01, formatter: value => Fmt.bps(value, 2),
});
const RATIO_FORMAT = Object.freeze({
  type: 'custom', minMove: 0.01, formatter: value => `${Fmt.number(value, 2)}×`,
});
const CURSOR_HTML = '<span class="cursor" aria-hidden="true"></span>';

const windowLabel = () => document.getElementById('hours').selectedOptions[0]?.textContent || '';
const stat = (label, value, { tone = 'text-body', meta = '' } = {}) => `<div class="kpi">
    <p class="kpi-label">${label}</p>
    <p class="kpi-value ${tone}">${value}</p>${meta ? `<p class="kpi-meta">${meta}</p>` : ''}</div>`;

function availableAssets(forMarket = market) {
  return [...new Set(
    SERIES.filter(series => series.market_type === forMarket).map(series => series.asset)
  )].sort();
}

function chartAssets() {
  return [...new Set(SERIES.map(series => series.asset))].sort();
}

function selectedAssets() {
  const available = chartAssets();
  const saved = dashboardFilters.get(SHARED_ASSETS_KEY);
  if (saved === null) return available.includes('BTC') ? ['BTC'] : available.slice(0, 1);
  return dashboardFilters.getList(SHARED_ASSETS_KEY)
    .filter(asset => available.includes(asset))
    .slice(0, MAX_CHARTS);
}

function saveSelectedAssets(assets) {
  dashboardFilters.setList(SHARED_ASSETS_KEY, assets);
}

function availableExchanges() {
  const exchanges = new Set([...VENUE_ORDER, ...SERIES.map(series => series.exchange)]);
  Object.values(discoveredExchanges)
    .forEach(discovered => discovered.forEach(exchange => exchanges.add(exchange)));
  return [...exchanges].sort((a, b) => {
    const ai = VENUE_ORDER.indexOf(a);
    const bi = VENUE_ORDER.indexOf(b);
    return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi) || a.localeCompare(b);
  });
}

function selectedExchanges() {
  const available = availableExchanges();
  if (dashboardFilters.get(SHARED_EXCHANGES_KEY) === null) return available;
  return dashboardFilters.getList(SHARED_EXCHANGES_KEY)
    .filter(exchange => available.includes(exchange));
}

function saveSelectedExchanges(exchanges) {
  dashboardFilters.setList(SHARED_EXCHANGES_KEY, exchanges);
}

async function fetchCandles(series, hours, { forceRefresh = false } = {}) {
  const qs = new URLSearchParams({
    exchange: series.exchange,
    market_type: series.market_type,
    symbol: series.symbol,
    interval: series.interval,
    hours,
    limit: 5000,
  });
  return Overseer.requestJSON(`/api/candles?${qs}`, { forceRefresh });
}

async function fetchVolatility(series, hours, windowHours, { forceRefresh = false } = {}) {
  const qs = new URLSearchParams({
    exchange: series.exchange,
    market_type: series.market_type,
    symbol: series.symbol,
    hours,
    window_hours: windowHours,
  });
  return Overseer.requestJSON(`/api/realised-volatility?${qs}`, { forceRefresh });
}

async function fetchSpreads(asset, hours, { forceRefresh = false } = {}) {
  const qs = new URLSearchParams({ asset, market, hours });
  return Overseer.requestJSON(`/api/orderbook-spreads?${qs}`, { forceRefresh });
}

class MarketChartCard {
  constructor(root, asset, { stats = null } = {}) {
    this.root = root;
    this.asset = asset;
    this.stats = stats;
    this.status = root.querySelector('[data-chart-status]');
    this.retry = root.querySelector('[data-chart-retry]');
    this.volatilityCloses = {};
    this.volatilityWindow = volatilityHours;
    this.legend = root.querySelector('[data-chart-legend]');
    this.title = root.querySelector('[data-chart-asset]');
    this.subtitle = root.querySelector('[data-chart-subtitle]');
    this.chart = LightweightCharts.createChart(
      root.querySelector('[data-chart-canvas]'),
      chartOptions(),
    );
    this.handles = [];
    this.data = {};
    this.metric = chartMetric;
    this.loadVersion = 0;
    this.availability = root.querySelector('[data-chart-availability]');
    this.availabilityText = document.createTextNode('');
    this.availabilityDetail = document.createElement('span');
    this.availabilityDetail.className = 'sr-only';
    this.availability.replaceChildren(this.availabilityText, this.availabilityDetail);
    this.priceFormat = formatChartNumber;
    this.readout = legendReadout(this.chart, this.legend);
    // Fills the plot when every venue with data is hidden, instead of a blank canvas.
    this.empty = document.createElement('div');
    this.empty.className = 'empty-overlay pointer-events-none hidden';
    this.emptyTitle = document.createElement('p');
    this.emptyTitle.className = 'empty-state-title';
    this.emptyHint = document.createElement('p');
    this.emptyHint.className = 'empty-state-hint';
    this.empty.append(this.emptyTitle, this.emptyHint);
    root.querySelector('[data-chart-canvas]').append(this.empty);
    this.retry?.addEventListener('click', () => this.load());
  }

  setStatus(message, { retry = false, busy = false } = {}) {
    this.status.textContent = message;
    if (busy) this.status.insertAdjacentHTML('beforeend', CURSOR_HTML);
    this.retry?.classList.toggle('hidden', !retry);
  }

  rowValue(row) {
    if (!row) return Fmt.DASH;
    if (this.metric === 'spread') return Fmt.bps(row.spread_bps, 2);
    if (this.metric === 'volatility') {
      const coverage = 100 * row.returns / (this.volatilityWindow * 60);
      return `${Fmt.percent(row.rv_pct, 2)} · ${Fmt.percent(coverage, 0)} cov.`;
    }
    return this.priceFormat(row.close);
  }

  // The row the legend shows off the plot: the latest, or for volatility the
  // last complete window (an incomplete one leaves a gap, not a point).
  latestRow(venue) {
    const rows = this.data[venue];
    if (this.metric === 'volatility') return rows.findLast(row => row.rv_pct !== null) ?? rows.at(-1);
    return rows.at(-1);
  }

  updateAvailability(visible) {
    const selected = selectedExchanges();
    const available = visible.filter(venue => this.metric !== 'volatility' ||
      this.data[venue].some(row => row.rv_pct !== null));
    const missing = selected.filter(venue => !available.includes(venue));
    const partial = this.metric === 'volatility' ? visible.filter(venue => {
      const last = this.data[venue].at(-1);
      return last.rv_pct === null && last.returns > 0;
    }).length : 0;
    this.availabilityText.textContent = selected.length
      ? `${available.length}/${selected.length} selected venues available${
        partial ? ` · ${partial} latest ${partial === 1 ? 'window' : 'windows'} incomplete` : ''}`
      : 'No venues selected.';
    const missingText = missing.length ? `Unavailable: ${missing.map(labelFor).join(', ')}.` : '';
    this.availability.title = missingText;
    const latest = visible.map(venue =>
      `${labelFor(venue)}: ${this.rowValue(this.latestRow(venue))}`).join('; ');
    this.availabilityDetail.textContent =
      `${missingText ? ` ${missingText}` : ''}${latest ? ` Latest values: ${latest}.` : ''}`;
  }

  clearSeries() {
    this.handles.forEach(handle => this.chart.removeSeries(handle));
    this.handles = [];
  }

  async load({ forceRefresh = false } = {}) {
    const version = ++this.loadVersion;
    const requestedMetric = chartMetric;
    const requestedWindow = volatilityHours;
    const isVolatility = requestedMetric === 'volatility';
    const hours = document.getElementById('hours').value;
    this.setStatus(Object.keys(this.data).length ? 'Refreshing' : 'Loading', { busy: true });

    if (requestedMetric === 'spread') {
      try {
        const payload = await fetchSpreads(this.asset, hours, { forceRefresh });
        if (version !== this.loadVersion) return;
        payload.series.forEach(series => {
          discoveredExchanges[payload.market].add(series.exchange);
        });
        renderExchangeSummary();
        this.metric = requestedMetric;
        this.data = Object.fromEntries(
          payload.series.map(series => [series.exchange, series.points])
        );
        this.render();
        if (Object.values(this.data).some(rows => rows.length)) {
          this.setStatus('');
          dashboardState.ready();
        } else {
          this.setStatus('No spread samples');
          if (dashboardView === 'overview') {
            dashboardState.empty(
              'No spread samples for this market in this window. Choose another asset or a longer window.',
            );
          }
        }
      } catch (error) {
        if (version !== this.loadVersion) return;
        this.metric = requestedMetric;
        this.data = {};
        this.render();
        this.setStatus('Load failed', { retry: true });
        if (dashboardView === 'overview') {
          dashboardState.error('Spread series', () => this.load());
        }
      }
      return;
    }

    const byVenue = {};
    for (const series of SERIES.filter(
      series => series.market_type === market && series.asset === this.asset
    )) {
      if (isVolatility && series.interval !== '1m') continue;
      if (!byVenue[series.exchange] || series.interval === '1m') {
        byVenue[series.exchange] = series;
      }
    }
    const venues = Object.keys(byVenue);
    if (!venues.length) {
      this.metric = requestedMetric;
      this.data = {};
      this.render();
      this.setStatus(`No ${market} series`);
      return;
    }
    const results = await Promise.allSettled(
      venues.map(venue => isVolatility
        ? fetchVolatility(byVenue[venue], hours, requestedWindow, { forceRefresh })
        : fetchCandles(byVenue[venue], hours, { forceRefresh }))
    );
    if (version !== this.loadVersion) return;
    this.metric = requestedMetric;
    this.volatilityWindow = requestedWindow;
    this.volatilityCloses = {};
    this.data = Object.fromEntries(
      venues.flatMap((venue, index) => {
        if (results[index].status !== 'fulfilled') return [];
        const payload = results[index].value;
        if (isVolatility) this.volatilityCloses[venue] = payload.last_close_time;
        return [[venue, isVolatility ? payload.points : payload]];
      })
    );
    this.render();
    const failures = results.filter(result => result.status === 'rejected').length;
    const hasBars = Object.values(this.data).some(rows =>
      isVolatility ? rows.some(row => row.rv_pct !== null) : rows.length
    );
    if (hasBars) {
      this.setStatus(
        failures
          ? `${failures} ${failures === 1 ? 'venue' : 'venues'} unavailable`
          : '',
        { retry: failures > 0 },
      );
      dashboardState.ready();
    } else if (failures === results.length) {
      this.setStatus('Load failed', { retry: true });
      if (dashboardView === 'overview') {
        dashboardState.error(isVolatility ? 'Realised volatility' : 'Price series', () => this.load());
      }
    } else {
      this.setStatus(isVolatility ? 'No complete windows' : 'No bars in window');
      if (dashboardView === 'overview') {
        dashboardState.empty(
          isVolatility
            ? 'No complete volatility windows in this range. Choose a shorter rolling window or a longer window.'
            : 'No price bars for this market in this window. Choose another asset or a longer window.',
        );
      }
    }
  }

  render() {
    this.clearSeries();
    const ordered = [...Object.keys(VENUE_COLORS), ...Object.keys(this.data)]
      .filter((venue, index, all) => all.indexOf(venue) === index && this.data[venue]?.length);
    const allowed = new Set(selectedExchanges());
    const visible = ordered.filter(venue => allowed.has(venue));
    const isSpread = this.metric === 'spread';
    const isVolatility = this.metric === 'volatility';
    const metricTitle = isSpread ? 'Spread' : isVolatility ? 'Realised volatility' : 'Price';
    this.title.textContent = this.asset;
    this.subtitle.textContent = `${metricTitle} · ${windowLabel()}`;
    this.root.dataset.panelLabel = `${this.asset} ${metricTitle.toLowerCase()} chart`;
    const candles = !isSpread && !isVolatility && visible.length === 1;
    // Data exists but no selected venue has any: say so on the plot. Missing
    // data and failed loads are explained by the panel status instead.
    const hiddenOnly = !visible.length && (ordered.length > 0 || !allowed.size);
    this.empty.classList.toggle('hidden', !hiddenOnly);
    if (hiddenOnly) {
      this.emptyTitle.textContent = allowed.size ? 'No selected venue has data here' : 'No venues selected';
      this.emptyHint.textContent = 'Choose venues above, or [all].';
    }

    if (candles) {
      const series = this.chart.addCandlestickSeries({
        upColor: CHART_THEME.gain,
        downColor: CHART_THEME.loss,
        wickUpColor: CHART_THEME.gain,
        wickDownColor: CHART_THEME.loss,
        borderVisible: false,
        priceLineVisible: false,
        priceFormat: CHART_PRICE_FORMAT,
      });
      series.setData(this.data[visible[0]]);
      this.handles.push(series);
    } else {
      for (const venue of visible) {
        const options = {
          color: colorFor(venue),
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: false,
          priceFormat: isSpread ? SPREAD_FORMAT : isVolatility ? chartPercentFormat(2) : CHART_PRICE_FORMAT,
        };
        if (isVolatility) {
          // Separate segments prevent the library joining across missing windows.
          const timeline = this.chart.addLineSeries({
            ...options, lineVisible: false, crosshairMarkerVisible: false,
          });
          timeline.setData(this.data[venue].map(row => ({ time: row.time })));
          this.handles.push(timeline);
          let segment = [];
          const addSegment = () => {
            if (!segment.length) return;
            const line = this.chart.addLineSeries(options);
            line.setData(segment);
            this.handles.push(line);
            segment = [];
          };
          for (const row of this.data[venue]) {
            if (row.rv_pct === null) addSegment();
            else segment.push({ time: row.time, value: row.rv_pct });
          }
          addSegment();
          continue;
        }
        const series = this.chart.addLineSeries(options);
        series.setData(this.data[venue].map(row => ({
          time: row.time,
          value: isSpread ? row.spread_bps : row.close,
        })));
        this.handles.push(series);
      }
    }

    this.priceFormat = fixedFormatter(visible.map(venue => this.latestRow(venue)?.close));
    this.updateAvailability(visible);
    this.readout.set(visible.map(venue => ({
      label: labelFor(venue),
      color: colorFor(venue),
      shape: candles ? 'bar' : 'line',
      text: time => this.rowValue(time === null
        ? this.latestRow(venue)
        : rowAt(this.data[venue], time)),
    })));
    this.legend.classList.toggle('hidden', !visible.length);

    if (this.stats) {
      const reference = [...visible]
        .sort((a, b) => this.data[b].length - this.data[a].length)[0];
      if (reference) {
        const rows = this.data[reference];
        const last = rows[rows.length - 1];
        const venue = labelFor(reference);
        const span = `${windowLabel()} window`;
        if (isSpread) {
          const values = rows.map(row => row.spread_bps);
          const average = values.reduce((sum, value) => sum + value, 0) / values.length;
          this.stats.innerHTML =
            stat('Last', Fmt.bps(last.spread_bps, 2), { meta: venue }) +
            stat(`Average ${windowLabel()}`, Fmt.bps(average, 2), { meta: venue }) +
            stat('Widest', Fmt.bps(Math.max(...values), 2), { meta: span }) +
            stat('Tightest', Fmt.bps(Math.min(...values), 2), { meta: span });
        } else if (isVolatility) {
          const close = this.volatilityCloses[reference];
          this.stats.innerHTML =
            stat('Last', Fmt.percent(last.rv_pct, 2), { meta: venue }) +
            stat('Rolling window', `${this.volatilityWindow}h`, { meta: '1m closes' }) +
            stat('Coverage', Fmt.percent(100 * last.returns / (this.volatilityWindow * 60), 1),
                 { meta: 'latest window' }) +
            stat('Last close', close ? Fmt.time(close, { seconds: false }) : Fmt.DASH, { meta: venue });
        } else {
          const first = rows[0];
          const delta = (last.close - first.close) / first.close * 100;
          const high = Math.max(...rows.map(row => row.high));
          const low = Math.min(...rows.map(row => row.low));
          const price = fixedFormatter([last.close, high, low]);
          this.stats.innerHTML =
            stat('Last', price(last.close), { meta: venue }) +
            stat(`<span class="unit">${windowLabel()}</span> change`, Fmt.signedPercent(delta, 2), {
              tone: delta > 0 ? 'text-gain' : delta < 0 ? 'text-loss' : 'text-body',
              meta: 'since window open',
            }) +
            stat('High', price(high), { meta: span }) +
            stat('Low', price(low), { meta: span });
        }
      } else {
        // Same tiles, dashed, so the strip keeps its shape.
        const labels = isSpread ? ['Last', `Average ${windowLabel()}`, 'Widest', 'Tightest']
          : isVolatility ? ['Last', 'Rolling window', 'Coverage', 'Last close']
          : ['Last', `<span class="unit">${windowLabel()}</span> change`, 'High', 'Low'];
        const why = allowed.size ? 'No data from the selected venues' : 'No venues selected';
        this.stats.innerHTML = labels
          .map((label, index) => stat(label, Fmt.DASH, { tone: 'text-dim', meta: index ? '' : why }))
          .join('');
      }
    }
    if (visible.length) this.chart.timeScale().fitContent();
  }

  destroy() {
    this.loadVersion += 1;
    this.chart.remove();
  }
}

const overviewCard = new MarketChartCard(
  document.querySelector('[data-panel="overview-chart"]'),
  'BTC',
  {
    stats: document.getElementById('overview-stats'),
  },
);

function populateTokens() {
  const select = document.getElementById('token');
  const previous = select.value;
  const saved = dashboardFilters.get('token');
  const tokens = availableAssets();
  select.innerHTML = tokens.map(token => `<option value="${token}">${token}</option>`).join('');
  if (tokens.includes(previous)) select.value = previous;
  else if (tokens.includes(saved)) select.value = saved;
  else if (tokens.includes('BTC')) select.value = 'BTC';
  dashboardFilters.set('token', select.value);
  overviewCard.asset = select.value;
}

function createMultiCard(asset) {
  const root = document.createElement('section');
  root.dataset.panel = `chart-${market}-${asset}`;
  root.dataset.panelLabel = `${asset} chart`;
  root.dataset.chartCard = '';
  root.className = 'chart-card dashboard-market-chart flex flex-col overflow-hidden';
  root.innerHTML = `
    <div class="panel-header">
      <h2 class="panel-title"><span data-chart-asset>${asset}</span><span data-chart-subtitle
          class="panel-subtitle"></span></h2>
      <div class="panel-actions" role="group" aria-label="${asset} chart controls">
        <span data-chart-status class="mr-1 max-w-28 truncate font-mono text-label text-faint"
              role="status"></span>
        <button type="button" data-chart-retry class="bracket-btn hidden">Retry</button>
        <button type="button" data-max class="bracket-btn"></button>
        <button type="button" data-collapse class="bracket-btn"></button>
        <button type="button" data-remove-asset="${asset}" aria-label="Remove ${asset} chart"
                title="Remove ${asset} chart" class="bracket-btn bracket-btn-danger">×</button>
      </div>
    </div>
    <div data-panel-body class="flex min-h-0 flex-1 flex-col">
      <div data-chart-legend class="legend hidden px-3 pt-2 sm:px-4"></div>
      <div data-chart-canvas aria-hidden="true" class="relative min-h-0 flex-1"></div>
      <div class="shrink-0 border-t border-line-soft">
        <p data-chart-availability class="meta-line mx-3 py-2 sm:mx-4"></p>
      </div>
    </div>`;
  document.getElementById('chart-grid').append(root);
  return new MarketChartCard(root, asset);
}

function renderAssetSummary() {
  const assets = selectedAssets();
  const summary = assets.length ? assets.join(', ') : 'Choose assets';
  const trigger = document.getElementById('asset-picker-trigger');
  document.getElementById('asset-picker-label').textContent = summary;
  document.getElementById('asset-picker-count').textContent =
    `${assets.length} / ${MAX_CHARTS}`;
  trigger.title = assets.length ? `Selected charts: ${summary}` : 'Choose chart assets';
  trigger.setAttribute('aria-label', `${trigger.title}. ${assets.length} of ${MAX_CHARTS} selected.`);
  syncChartFilters();
}

function renderAssetOptions() {
  const options = document.getElementById('asset-options');
  const query = document.getElementById('asset-search').value.trim().toLowerCase();
  const atLimit = stagedAssets.size >= MAX_CHARTS;
  const rows = new Map(
    [...options.querySelectorAll('label[data-asset]')]
      .map(row => [row.dataset.asset, row]),
  );
  let matchCount = 0;
  for (const asset of chartAssets()) {
    let row = rows.get(asset);
    if (!row) {
      row = document.createElement('label');
      row.dataset.asset = asset;
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = asset;
      input.className = 'h-4 w-4 accent-phosphor disabled:cursor-not-allowed';
      const name = document.createElement('span');
      name.textContent = asset;
      row.append(input, name);
      options.append(row);
    }
    const checked = stagedAssets.has(asset);
    const disabled = atLimit && !checked;
    const matches = !query || asset.toLowerCase().includes(query);
    const input = row.querySelector('input');
    input.checked = checked;
    input.disabled = disabled;
    row.className = `${matches ? 'flex' : 'hidden'} min-h-11 items-center gap-3 rounded px-2 font-mono text-data
      ${disabled ? 'cursor-not-allowed bg-panel/30 text-faint'
                 : 'cursor-pointer hover:bg-panel2'}`;
    row.querySelector('span').className = disabled ? 'text-faint' : 'text-body';
    if (matches) matchCount++;
  }
  let empty = options.querySelector('[data-asset-empty]');
  if (!empty) {
    empty = document.createElement('div');
    empty.dataset.assetEmpty = '';
    empty.className = 'px-2 py-6 text-center font-mono text-data text-dim';
    empty.textContent = 'No assets match';
    options.append(empty);
  }
  empty.hidden = matchCount > 0;
}

function setAssetPicker(open, restoreFocus = false) {
  const picker = document.getElementById('asset-picker');
  const trigger = document.getElementById('asset-picker-trigger');
  picker.classList.toggle('hidden', !open);
  trigger.setAttribute('aria-expanded', String(open));
  if (open) {
    stagedAssets = new Set(selectedAssets());
    document.getElementById('asset-search').value = '';
    renderAssetOptions();
    document.getElementById('asset-search').focus();
  } else if (restoreFocus) {
    trigger.focus();
  }
}

function renderExchangeSummary() {
  const available = availableExchanges();
  const selected = selectedExchanges();
  const legend = document.getElementById('exchange-legend');
  const activeVenues = new Set(selected);
  const buttons = new Map(
    [...legend.querySelectorAll('[data-exchange]')]
      .map(button => [button.dataset.exchange, button]),
  );
  available.forEach((venue, index) => {
    let button = buttons.get(venue);
    if (!button) {
      button = document.createElement('button');
      button.type = 'button';
      button.className = 'chip';
      button.dataset.exchange = venue;
      button.style.setProperty('--swatch', colorFor(venue));
      const swatch = document.createElement('span');
      swatch.className = 'chip-swatch';
      const label = document.createElement('span');
      label.textContent = labelFor(venue);
      button.append(swatch, label);
    }
    buttons.delete(venue);
    const active = activeVenues.has(venue);
    button.setAttribute('aria-pressed', String(active));
    button.title = `${labelFor(venue)}: ${active ? 'shown' : 'hidden'} on every chart`;
    if (legend.children[index] !== button) {
      legend.insertBefore(button, legend.children[index] ?? null);
    }
  });
  buttons.forEach(button => button.remove());
  syncChartFilters();
}

function describeChartFilters() {
  const assets = selectedAssets();
  const assetSummary = dashboardView === 'overview'
    ? document.getElementById('token').value || 'no asset'
    : assets.length === 1 ? assets[0] : `${assets.length} assets`;
  const venues = selectedExchanges().length;
  const metric = chartMetric === 'volatility' ? `volatility (${volatilityHours}h)` : chartMetric;
  return `${market} · ${assetSummary} · ${metric} · ${windowLabel()} · ${venues} ${venues === 1 ? 'venue' : 'venues'}`;
}

function renderMarketCards() {
  overviewCard.render();
  for (const card of multiCards.values()) card.render();
}

function syncMultiCards() {
  const grid = document.getElementById('chart-grid');
  const assets = selectedAssets();
  grid.dataset.chartCount = String(assets.length);
  for (const [asset, card] of multiCards) {
    if (!assets.includes(asset)) {
      card.destroy();
      card.root.remove();
      multiCards.delete(asset);
    }
  }
  for (const asset of assets) {
    if (!multiCards.has(asset)) multiCards.set(asset, createMultiCard(asset));
    grid.append(multiCards.get(asset).root);
  }
  document.getElementById('multi-chart-empty').classList.toggle('hidden', assets.length > 0);
  refreshPanels();
}

async function loadActiveCharts({ forceRefresh = false } = {}) {
  if (dashboardView === 'overview') {
    overviewCard.asset = document.getElementById('token').value;
    await overviewCard.load({ forceRefresh });
    return;
  }
  syncMultiCards();
  await Promise.all(
    [...multiCards.values()].map(card => card.load({ forceRefresh })),
  );
}

function setSegmentState(button, active) {
  button.setAttribute('aria-pressed', String(active));
}

function setDashboardView(nextView, reload = true) {
  dashboardView = nextView;
  dashboardFilters.set('view', nextView);
  const isCharts = nextView === 'charts';
  document.getElementById('overview-view').classList.toggle('hidden', isCharts);
  document.getElementById('charts-view').classList.toggle('hidden', !isCharts);
  document.getElementById('token-select-wrap').classList.toggle('hidden', isCharts);
  document.getElementById('asset-picker-wrap').classList.toggle('hidden', !isCharts);
  setSegmentState(document.getElementById('view-overview'), !isCharts);
  setSegmentState(document.getElementById('view-charts'), isCharts);
  setAssetPicker(false);
  if (isCharts) syncMultiCards();
  syncChartFilters();
  refreshPanels();
  if (reload) {
    loadActiveCharts();
    if (!isCharts) {
      loadOverview();
      loadBasis();
    }
  }
}

function setMetric(nextMetric, reload = true) {
  chartMetric = nextMetric;
  dashboardFilters.set('metric', nextMetric);
  setSegmentState(document.getElementById('metric-price'), nextMetric === 'price');
  const isSpread = nextMetric === 'spread';
  const isVolatility = nextMetric === 'volatility';
  const controls = document.getElementById('volatility-controls');
  controls.classList.toggle('hidden', !isVolatility);
  document.getElementById('dashboard-title').textContent =
    isSpread ? 'Bid–ask spreads' : isVolatility ? 'Realised volatility' : 'Cross-venue prices';
  setSegmentState(document.getElementById('metric-spread'), isSpread);
  setSegmentState(document.getElementById('metric-volatility'), isVolatility);
  document.querySelectorAll('[data-volatility-hours]').forEach(button => {
    setSegmentState(button, Number(button.dataset.volatilityHours) === volatilityHours);
  });
  syncChartFilters();
  if (reload) loadActiveCharts();
}


function setMarket(nextMarket, reload = true) {
  market = nextMarket;
  dashboardFilters.set('market', nextMarket);
  setSegmentState(document.getElementById('mkt-perp'), nextMarket === 'perp');
  setSegmentState(document.getElementById('mkt-spot'), nextMarket === 'spot');
  setAssetPicker(false);
  for (const card of multiCards.values()) {
    card.destroy();
    card.root.remove();
  }
  multiCards.clear();
  populateTokens();
  renderAssetSummary();
  renderExchangeSummary();
  if (dashboardView === 'charts') syncMultiCards();
  if (reload) loadActiveCharts();
}

const chartFiltersSummary = chartFilters.querySelector(':scope > summary');
chartFilters.addEventListener('toggle', () => {
  if (chartFilters.open) return;
  setAssetPicker(false);
  if (chartFilters.contains(document.activeElement)) chartFiltersSummary.focus();
});

document.getElementById('view-overview').addEventListener('click', () => setDashboardView('overview'));
document.getElementById('view-charts').addEventListener('click', () => setDashboardView('charts'));
document.getElementById('mkt-perp').addEventListener('click', () => setMarket('perp'));
document.getElementById('mkt-spot').addEventListener('click', () => setMarket('spot'));
document.getElementById('metric-price').addEventListener('click', () => setMetric('price'));
document.getElementById('metric-spread').addEventListener('click', () => setMetric('spread'));
document.getElementById('metric-volatility').addEventListener('click', () => setMetric('volatility'));
document.getElementById('token').addEventListener('change', event => {
  dashboardFilters.set('token', event.target.value);
  syncChartFilters();
  loadActiveCharts();
});
document.getElementById('hours').addEventListener('change', event => {
  dashboardFilters.set('hours', event.target.value);
  syncChartFilters();
  loadActiveCharts();
  if (dashboardView === 'overview') loadVolumeOiRatio();
});
document.getElementById('volatility-controls').addEventListener('click', event => {
  const button = event.target.closest('[data-volatility-hours]');
  if (!button) return;
  volatilityHours = Number(button.dataset.volatilityHours);
  dashboardFilters.set('volatility-hours', volatilityHours);
  setMetric('volatility');
});
document.getElementById('asset-picker-trigger').addEventListener('click', () => {
  const open = document.getElementById('asset-picker-trigger').getAttribute('aria-expanded') === 'true';
  setAssetPicker(!open, open);
});
function setAllVenues(show) {
  // No saved list means every venue, including ones discovered later.
  if (show) dashboardFilters.set(SHARED_EXCHANGES_KEY, null);
  else saveSelectedExchanges([]);
  renderExchangeSummary();
  renderMarketCards();
}
document.getElementById('venue-all').addEventListener('click', () => setAllVenues(true));
document.getElementById('venue-none').addEventListener('click', () => setAllVenues(false));
document.getElementById('exchange-legend').addEventListener('click', event => {
  const button = event.target.closest('[data-exchange]');
  if (!button) return;
  const selected = new Set(selectedExchanges());
  const venue = button.dataset.exchange;
  selected.has(venue) ? selected.delete(venue) : selected.add(venue);
  saveSelectedExchanges([...selected]);
  renderExchangeSummary();
  renderMarketCards();
});
document.getElementById('asset-search').addEventListener('input', renderAssetOptions);
document.getElementById('asset-options').addEventListener('change', event => {
  if (event.target.type !== 'checkbox') return;
  event.target.checked
    ? stagedAssets.add(event.target.value)
    : stagedAssets.delete(event.target.value);
  renderAssetOptions();
});
document.getElementById('asset-clear').addEventListener('click', () => {
  stagedAssets.clear();
  renderAssetOptions();
});
document.getElementById('asset-apply').addEventListener('click', () => {
  saveSelectedAssets([...stagedAssets].slice(0, MAX_CHARTS));
  renderAssetSummary();
  setAssetPicker(false, true);
  syncMultiCards();
  loadActiveCharts();
});
document.addEventListener('click', event => {
  const remove = event.target.closest('[data-remove-asset]');
  if (remove) {
    saveSelectedAssets(selectedAssets().filter(asset => asset !== remove.dataset.removeAsset));
    renderAssetSummary();
    syncMultiCards();
    (chartFilters.open
      ? document.getElementById('asset-picker-trigger')
      : chartFiltersSummary).focus();
    return;
  }
  const assetWrapper = document.getElementById('asset-picker-wrap');
  if (!assetWrapper.contains(event.target) &&
      document.getElementById('asset-picker-trigger').getAttribute('aria-expanded') === 'true') {
    setAssetPicker(false);
  }
});
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  if (document.getElementById('asset-picker-trigger').getAttribute('aria-expanded') === 'true') {
    setAssetPicker(false, true);
  }
});


// ---- panel chrome: collapse (persisted) + maximise (transient) --------------
// Collapse folds a panel to its title row and survives reloads via localStorage.
// Maximise floats the panel over the page (Esc restores). The chart keeps
// working in both: autoSize re-measures whenever its container changes.
const PANELS_KEY = 'overseer.collapsed';
const collapsedPanels = new Set(JSON.parse(localStorage.getItem(PANELS_KEY) || '[]'));
let maxedPanel = null;

// One size control per panel: [+] makes it bigger (collapsed → normal →
// enlarged) and [−] smaller (enlarged → normal → collapsed). At either end
// the button that can't go further is disabled. Escape restores an enlarged
// panel; collapsed panels are remembered.
function refreshPanels() {
  document.querySelectorAll('[data-panel]').forEach(sec => {
    const id = sec.dataset.panel;
    const label = sec.dataset.panelLabel || `${id} panel`;
    const isMax = maxedPanel === id;
    const isCol = collapsedPanels.has(id) && !isMax;
    sec.querySelector('[data-panel-body]').classList.toggle('hidden', isCol);
    sec.classList.toggle('panel-collapsed', isCol);
    sec.classList.toggle('panel-maxed', isMax);
    const grow = sec.querySelector('[data-max]');
    if (grow) {
      const action = isCol ? `Reopen ${label}` : isMax ? `${label} is enlarged` : `Enlarge ${label}`;
      grow.textContent = '+';
      grow.disabled = isMax;
      grow.title = action;
      grow.setAttribute('aria-label', action);
    }
    const shrink = sec.querySelector('[data-collapse]');
    if (shrink) {
      const action = isMax ? `Restore ${label}` : isCol ? `${label} is collapsed` : `Collapse ${label}`;
      shrink.textContent = '−';
      shrink.disabled = isCol;
      shrink.title = action;
      shrink.setAttribute('aria-label', action);
      shrink.setAttribute('aria-expanded', String(!isCol));
    }
  });
}

document.addEventListener('click', e => {
  const btn = e.target.closest('[data-collapse],[data-max]');
  if (!btn || btn.disabled) return;
  const sec = btn.closest('[data-panel]');
  const id = sec.dataset.panel;
  const grow = btn.hasAttribute('data-max');
  if (grow) {
    if (collapsedPanels.has(id)) collapsedPanels.delete(id);
    else maxedPanel = id;
  } else if (maxedPanel === id) {
    maxedPanel = null;
  } else {
    collapsedPanels.add(id);
  }
  localStorage.setItem(PANELS_KEY, JSON.stringify([...collapsedPanels]));
  refreshPanels();
  // At a limit the pressed button is now disabled; keep focus on the control.
  if (btn.disabled) sec.querySelector(grow ? '[data-collapse]' : '[data-max]')?.focus();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && maxedPanel) { maxedPanel = null; refreshPanels(); }
});
refreshPanels();

const syncChartFilters = Overseer.filtersFold(chartFilters, describeChartFilters);
dashboardFilters.restoreSelect(document.getElementById('hours'));
populateTokens();
renderAssetSummary();
setMarket(market, false);
setMetric(chartMetric, false);
setDashboardView(dashboardView, false);
Overseer.poll(loadActiveCharts, 60);

// ---- market overview panels (venue volume + OI + carry spreads) -------------
function bar(label, value, max, color) {
  const w = max > 0 ? Math.max(1.5, value / max * 100) : 0;
  return `<div>
    <div class="mb-1 flex items-baseline justify-between gap-3 font-mono text-data">
      <span class="truncate text-body">${label}</span>
      <span class="tabular-nums text-dim">${Fmt.usdCompact(value)}</span></div>
    <div class="h-1.5 rounded bg-panel2">
      <div class="h-1.5 rounded ${color ? '' : 'bg-dim/60'}"
           style="width:${w}%${color ? `;background-color:${color}` : ''}"></div></div>
  </div>`;
}

function ensureVolumeOiChart() {
  if (volumeOiChart) return volumeOiChart;
  volumeOiChart = LightweightCharts.createChart(
    document.getElementById('volume-oi-chart'),
    chartOptions({ rightPriceScale: { minimumWidth: 68 } }),
  );
  volumeOiReadout = legendReadout(volumeOiChart, document.getElementById('volume-oi-legend'),
    { empty: 'No venues reported' });
  return volumeOiChart;
}

const ratioText = point => (point ? `${Fmt.number(point.ratio, 2)}×` : Fmt.DASH);

function orderedVolumeOiSeries(payload) {
  const byVenue = new Map(payload.series.map(item => [item.exchange, item]));
  return [...VENUE_ORDER, ...byVenue.keys()]
    .filter((venue, index, all) => all.indexOf(venue) === index && byVenue.has(venue))
    .map(venue => byVenue.get(venue));
}

function renderVolumeOiRatio(payload) {
  const chart = ensureVolumeOiChart();
  volumeOiSeries.forEach(series => chart.removeSeries(series));
  volumeOiSeries.clear();

  const ordered = orderedVolumeOiSeries(payload);
  for (const item of ordered) {
    const series = chart.addLineSeries({
      color: colorFor(item.exchange),
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerRadius: 4,
      priceFormat: RATIO_FORMAT,
    });
    series.setData(item.points.map(point => ({ time: point.time, value: point.ratio })));
    volumeOiSeries.set(item.exchange, series);
  }
  if (ordered.length) chart.timeScale().fitContent();

  volumeOiReadout.set(ordered.map(item => ({
    label: labelFor(item.exchange),
    color: colorFor(item.exchange),
    shape: 'line',
    text: time => ratioText(time === null ? item.points.at(-1) : rowAt(item.points, time)),
  })));

  document.getElementById('volume-oi-empty').classList.toggle('hidden', ordered.length > 0);

  const latest = ordered
    .map(item => [item.exchange, item.points[item.points.length - 1]?.ratio])
    .filter(([, ratio]) => ratio != null)
    .sort((a, b) => b[1] - a[1]);
  document.getElementById('volume-oi-summary').textContent = latest.length
    ? `Latest volume to open interest ratios. ${latest.map(
        ([venue, ratio]) => `${labelFor(venue)} ${Fmt.number(ratio, 2)} times`
      ).join(', ')}.`
    : 'No volume to open interest ratio history is available.';
}

async function loadVolumeOiRatio({ forceRefresh = false } = {}) {
  if (dashboardView !== 'overview') return;
  const requestId = ++volumeOiRequest;
  const status = document.getElementById('volume-oi-status');
  status.innerHTML = `Loading${CURSOR_HTML}`;
  const hours = document.getElementById('hours').value;
  try {
    const payload = await Overseer.requestJSON(
      `/api/volume-oi-ratio?${new URLSearchParams({ hours })}`,
      { forceRefresh },
    );
    if (requestId !== volumeOiRequest) return;
    renderVolumeOiRatio(payload);
    status.textContent = `${payload.bucket_minutes}m buckets`;
    payload.series.length
      ? dashboardState.ready('volume-oi')
      : dashboardState.empty(
          'No volume/OI ratio history for this window. Choose a longer window.',
          'volume-oi',
        );
  } catch (error) {
    if (requestId !== volumeOiRequest) return;
    status.textContent = 'Unavailable';
    dashboardState.error('Volume/OI ratio', loadVolumeOiRatio, 'volume-oi');
  }
}


async function loadOverview({ forceRefresh = false } = {}) {
  if (dashboardView !== 'overview') return;
  try {
    const vv = await Overseer.requestJSON('/api/venue-volume', { forceRefresh });
    const latest = vv.latest.sort((a, b) => b.total - a.total);
    const max = latest[0]?.total || 0;
    const cexTotal = latest.filter(v => ['binance','bybit'].includes(v.exchange))
                           .reduce((s, v) => s + v.total, 0);
    const all = latest.reduce((s, v) => s + v.total, 0);
    document.getElementById('cex-share').textContent =
      all ? Fmt.percent(cexTotal / all * 100, 1) : Fmt.DASH;
    document.getElementById('vol-bars').innerHTML = latest.length
      ? latest.map(v => bar(labelFor(v.exchange), v.total, max, colorFor(v.exchange))).join('')
      : `<div class="empty-state"><p class="empty-state-title">No venue volume yet</p>
           <p class="empty-state-hint">Daily totals arrive after 00:37 UTC.</p></div>`;
    dashboardState.ready('venue-volume');
  } catch (error) {
    dashboardState.error('Venue-volume summary', loadOverview, 'venue-volume');
  }

  try {
    const fu = await Overseer.requestJSON('/api/funding', { forceRefresh });
    const oi = Object.entries(fu).map(([coin, rows]) => [coin,
        rows.reduce((s, r) => s + (r.oi_notional || 0), 0)])
      .filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
    const oimax = oi[0]?.[1] || 0;
    document.getElementById('oi-bars').innerHTML = oi.length
      ? oi.map(([c, v]) => bar(c, v, oimax)).join('')
      : `<div class="empty-state"><p class="empty-state-title">No open interest yet</p>
           <p class="empty-state-hint">Perp venues have not reported open interest.</p></div>`;
    dashboardState.ready('open-interest');

  } catch (error) {
    dashboardState.error('Open-interest summary', loadOverview, 'open-interest');
  }

  await loadVolumeOiRatio({ forceRefresh });
}

Overseer.poll(loadOverview, 300);

// ---- basis snapshot: same-venue spot/perp pairs, latest bar -----------------
async function loadBasis({ forceRefresh = false } = {}) {
  if (dashboardView !== 'overview') return;
  const pairs = [];
  for (const s of SERIES.filter(x => x.market_type === 'spot')) {
    if (SERIES.some(p => p.market_type === 'perp' && p.exchange === s.exchange && p.symbol === s.symbol))
      pairs.push(s);
  }
  const shown = pairs.filter(p => p.exchange === 'binance').slice(0, 8);
  try {
    const results = await Promise.all(shown.map(async p => {
      const qs = new URLSearchParams({ spot_exchange: p.exchange, spot_symbol: p.symbol,
        perp_exchange: p.exchange, perp_symbol: p.symbol, interval: '1m', limit: 1 });
      const d = await Overseer.requestJSON(`/api/basis?${qs}`, { forceRefresh });
      return d.length ? { symbol: p.symbol, bps: d[d.length-1].basis_pct * 100 } : null;
    }));
    const rows = results.filter(Boolean).sort((a, b) => Math.abs(b.bps) - Math.abs(a.bps));
    document.getElementById('basis-rows').innerHTML = rows.length
      ? rows.map(r => `<tr>
          <td class="font-mono text-body">${r.symbol}</td>
          <td class="num ${r.bps > 0 ? 'text-gain' : r.bps < 0 ? 'text-loss' : 'text-body'}">${
            Fmt.number(r.bps, 1, { sign: true })}</td></tr>`).join('')
      : '<tr><td colspan="2" class="empty-cell">No paired spot and perp bars yet.</td></tr>';
    dashboardState.ready('basis-snapshot');
  } catch (error) {
    dashboardState.error('Basis snapshot', loadBasis, 'basis-snapshot');
  }
}


Overseer.poll(loadBasis, 60);
['vol-bars', 'oi-bars', 'basis-scroll']
  .forEach(id => Overseer.scrollFade(document.getElementById(id)));
})();
