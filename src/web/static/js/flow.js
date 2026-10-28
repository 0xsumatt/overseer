// Trade flow page. VENUE_ORDER / colorFor / labelFor / chartOptions /
// legendReadout / rowAt come from venues.js.
(() => {
const FLOW_CHART_OPTS = {
  crosshair: { mode: 0 },
  rightPriceScale: { scaleMargins: { top: 0.1, bottom: 0.08 } },
  handleScale: { axisPressedMouseMove: false },
};
const IMB_FORMAT = chartPercentFormat(0, true);
const COUNT_FORMAT = { type: 'custom', minMove: 1, formatter: v => v.toFixed(0) };

const imbChart = LightweightCharts.createChart(document.getElementById('chart-imb'), chartOptions(FLOW_CHART_OPTS));
const lrgChart = LightweightCharts.createChart(document.getElementById('chart-lrg'), chartOptions(FLOW_CHART_OPTS));
const zeroLine = imbChart.addLineSeries({ color: CHART_THEME.zero, lineWidth: 1, priceFormat: IMB_FORMAT,
  priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
const imbReadout = legendReadout(imbChart, document.getElementById('legend'));
const lrgReadout = legendReadout(lrgChart, document.getElementById('legend-lrg'));

const flowFilters = Overseer.filterState('flow');
const imbSeries = {}, lrgSeries = {};
let data = { series: {}, settlements: {} };
const hidden = new Set(flowFilters.getList('venues'));
const flowState = Overseer.pageState();
let flowRequest = 0;

const windowLabel = () => document.getElementById('hours').selectedOptions[0].textContent;
const sideTone = (v) => v == null || v === 0 ? 'text-body' : v > 0 ? 'text-long' : 'text-short';
const imbText = (v) => Fmt.signedPercent(v, 1);

const presentVenues = () =>
  VENUE_ORDER.filter(v => data.series[v] && data.series[v].length);
const activeVenues = () => presentVenues().filter(v => !hidden.has(v));

const syncFilters = Overseer.filtersFold(document.getElementById('flow-filters'),
  () => `${document.getElementById('asset').value} · ${windowLabel()} · ${activeVenues().length} venues`);

async function load({ forceRefresh = false } = {}) {
  const request = ++flowRequest;
  const asset = document.getElementById('asset').value;
  const hours = document.getElementById('hours').value;
  flowState.loading('Loading trade flow');
  try {
    const next = await Overseer.requestJSON(
      `/api/trade-flow?asset=${encodeURIComponent(asset)}&hours=${hours}`,
      { forceRefresh },
    );
    if (request !== flowRequest) return;
    data = next;
    data.series = data.series || {};
    renderFilters();
    render();
    if (presentVenues().length) flowState.ready();
    else flowState.empty(`No trades for ${asset} in ${windowLabel()}. Choose another asset or a longer window.`);
  } catch (error) {
    if (request === flowRequest) flowState.error('Trade flow', load);
  }
}

function renderFilters() {
  // A venue keeps its colour whether or not it is selected, so toggling never
  // reassigns hues to the survivors.
  const filterHost = document.getElementById('venue-filters');
  const focusedVenue = filterHost.contains(document.activeElement)
    ? document.activeElement.dataset.venue : null;
  filterHost.innerHTML = presentVenues().map(v => `
    <button type="button" class="chip" data-venue="${v}" aria-pressed="${!hidden.has(v)}"
            style="--swatch:${colorFor(v)}"><span class="chip-swatch"></span>${labelFor(v)}</button>`
  ).join('') || '<span class="font-mono text-data text-faint">No venues reporting</span>';
  filterHost.querySelectorAll('.chip').forEach(button => button.onclick = () => {
    const v = button.dataset.venue;
    hidden.has(v) ? hidden.delete(v) : hidden.add(v);
    button.setAttribute('aria-pressed', String(!hidden.has(v)));
    flowFilters.setList('venues', hidden);
    render();
  });
  if (focusedVenue) {
    Array.from(filterHost.querySelectorAll('.chip'))
      .find(button => button.dataset.venue === focusedVenue)?.focus({ preventScroll: true });
  }
}

function clearSeries() {
  Object.values(imbSeries).forEach(s => imbChart.removeSeries(s));
  Object.values(lrgSeries).forEach(s => lrgChart.removeSeries(s));
  for (const key of Object.keys(imbSeries)) delete imbSeries[key];
  for (const key of Object.keys(lrgSeries)) delete lrgSeries[key];
}

function renderEmpty(venues) {
  let title = '', hint = '';
  if (!venues.length) {
    if (presentVenues().length) {
      title = 'All venues hidden.';
      hint = 'Select a venue to show its flow.';
    } else {
      title = `No trades for ${document.getElementById('asset').value} in ${windowLabel()}.`;
      hint = 'Choose another asset or a longer window.';
    }
  }
  for (const id of ['empty-imb', 'empty-lrg']) {
    const box = document.getElementById(id);
    box.classList.toggle('hidden', !title);
    box.querySelector('[data-empty-title]').textContent = title;
    box.querySelector('[data-empty-hint]').textContent = hint;
  }
}

function render() {
  clearSeries();
  const venues = activeVenues();
  renderEmpty(venues);

  for (const venue of venues) {
    const rows = data.series[venue], color = colorFor(venue);
    imbSeries[venue] = imbChart.addLineSeries({
      color, lineWidth: 2, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerRadius: 4, priceFormat: IMB_FORMAT,
    });
    imbSeries[venue].setData(rows.filter(d => d.imbalance_pct != null)
      .map(d => ({ time: d.time, value: d.imbalance_pct })));

    lrgSeries[venue] = lrgChart.addLineSeries({
      color, lineWidth: 2, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerRadius: 4, priceFormat: COUNT_FORMAT,
    });
    lrgSeries[venue].setData(rows.map(d => ({ time: d.time, value: d.large_trades ?? 0 })));

    const marks = (data.settlements || {})[venue] || [];
    if (marks.length) imbSeries[venue].setMarkers(marks.map(t => ({
      time: t, position: 'inBar', color, shape: 'arrowDown', size: 0, text: '⋮' })));
  }
  // keep the zero baseline spanning whatever is visible
  const anyRows = venues.length ? data.series[venues[0]] : [];
  zeroLine.setData(anyRows.map(d => ({ time: d.time, value: 0 })));

  renderTiles(venues);
  renderLegends(venues);
  renderTable(venues);
  syncFilters();
  imbChart.timeScale().fitContent();
  lrgChart.timeScale().fitContent();
}

function renderTiles(venues) {
  let rows = 0, vol = 0, large = 0, buy = 0, sell = 0, maxTrade = null, maxVenue = null;
  for (const v of venues) for (const d of data.series[v]) {
    rows += 1;
    vol += d.volume || 0; large += d.large_trades || 0;
    buy += d.buy_volume || 0; sell += d.sell_volume || 0;
    if (d.max_trade_notional != null && (maxTrade == null || d.max_trade_notional > maxTrade)) {
      maxTrade = d.max_trade_notional; maxVenue = v;
    }
  }
  const asset = document.getElementById('asset').value;
  const imb = (buy + sell) ? (buy - sell) / (buy + sell) * 100 : null;
  const tiles = [
    ['Window volume', rows ? Fmt.compact(vol) : Fmt.DASH, 'text-body', `${asset} · ${windowLabel()}`],
    ['Net imbalance', imbText(imb), sideTone(imb), 'Taker buys vs sells'],
    ['Large trades', rows ? Fmt.number(large, 0) : Fmt.DASH, 'text-body', '$10K+ notional'],
    ['Largest trade', maxTrade ? Fmt.usdCompact(maxTrade) : Fmt.DASH, 'text-body',
     maxTrade ? labelFor(maxVenue) : 'Single fill notional'],
  ];
  document.getElementById('tiles').innerHTML = tiles.map(([label, value, tone, meta]) => `
    <div class="kpi">
      <p class="kpi-label">${label}</p>
      <p class="kpi-value ${tone}">${value}</p>
      <p class="kpi-meta">${meta}</p>
    </div>`).join('');
}

// Direct-labelled with values that follow the crosshair, so the reading never
// depends on telling two hues apart.
function readoutEntries(venues, valueOf, format) {
  return venues.map(v => ({
    label: labelFor(v), color: colorFor(v), shape: 'line',
    text: (time) => {
      const rows = data.series[v];
      const d = time === null ? rows[rows.length - 1] : rowAt(rows, time);
      return format(d ? valueOf(d) : null);
    },
  }));
}

function renderLegends(venues) {
  imbReadout.set(readoutEntries(venues, d => d.imbalance_pct, imbText));
  lrgReadout.set(readoutEntries(venues, d => d.large_trades, v => Fmt.number(v, 0)));
}

function matchingRows(venues) {
  const minImb = +document.getElementById('f-imb').value;
  const minLrg = +document.getElementById('f-lrg').value;
  const out = [];
  for (const v of venues) for (const d of data.series[v]) {
    if (Math.abs(d.imbalance_pct ?? 0) < minImb) continue;
    if ((d.large_trades || 0) < minLrg) continue;
    out.push([v, d]);
  }
  out.sort((a, b) => b[1].time - a[1].time);
  return out;
}

function renderTable(venues) {
  const rows = matchingRows(venues);
  const displayedRows = rows.slice(0, 400);
  document.getElementById('match-count').textContent = Fmt.number(rows.length, 0);
  document.getElementById('detail-count').textContent =
    `Latest ${Fmt.number(displayedRows.length, 0)} of ${Fmt.number(rows.length, 0)}`;
  const emptyText = venues.length
    ? 'No minutes match these thresholds. Lower |imbalance| or large trades / min.'
    : 'No venue minutes to show. Select a venue or choose another asset.';
  document.getElementById('table-body').innerHTML = displayedRows.map(([v, d]) => `
    <tr>
      <td class="whitespace-nowrap tabular-nums text-faint">${Fmt.time(d.time, { seconds: false })}</td>
      <td class="whitespace-nowrap"><span class="legend-swatch align-middle" data-shape="dot"
            style="--swatch:${colorFor(v)}"></span>
        <span class="ml-2 align-middle text-body">${labelFor(v)}</span></td>
      <td class="num">${Fmt.number(d.trades, 0)}</td>
      <td class="num">${Fmt.compact(d.volume)}</td>
      <td class="num ${d.imbalance_pct == null ? '' : sideTone(d.imbalance_pct)}">${imbText(d.imbalance_pct)}</td>
      <td class="num">${Fmt.number(d.large_trades, 0)}</td>
      <td class="num ${d.large_imbalance_pct == null ? '' : sideTone(d.large_imbalance_pct)}">${imbText(d.large_imbalance_pct)}</td>
      <td class="num">${Fmt.usdCompact(d.max_trade_notional)}</td>
    </tr>`).join('') ||
    `<tr><td colspan="8" class="empty-cell">${emptyText}</td></tr>`;
}

let syncing = false;
function sync(from, to) {
  from.timeScale().subscribeVisibleLogicalRangeChange(r => {
    if (syncing || !r) return;
    syncing = true; to.timeScale().setVisibleLogicalRange(r); syncing = false;
  });
}
sync(imbChart, lrgChart); sync(lrgChart, imbChart);

for (const id of ['asset', 'hours', 'f-imb', 'f-lrg']) {
  flowFilters.restoreSelect(document.getElementById(id));
}
syncFilters();
document.getElementById('asset').onchange = event => {
  flowFilters.set('asset', event.target.value);
  syncFilters();
  load();
};
document.getElementById('hours').onchange = event => {
  flowFilters.set('hours', event.target.value);
  syncFilters();
  load();
};
document.getElementById('f-imb').onchange = event => {
  flowFilters.set('f-imb', event.target.value);
  renderTable(activeVenues());
};
document.getElementById('f-lrg').onchange = event => {
  flowFilters.set('f-lrg', event.target.value);
  renderTable(activeVenues());
};
document.getElementById('table-toggle').onclick = (e) => {
  const wrap = document.getElementById('table-wrap');
  wrap.classList.toggle('hidden');
  e.currentTarget.setAttribute('aria-expanded', String(!wrap.classList.contains('hidden')));
};
Overseer.poll(load, 60);
})();
