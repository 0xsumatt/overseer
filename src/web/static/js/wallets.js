// Tracked wallets page. colorFor / labelFor / chartOptions / legendReadout /
// fixedFormatter / rowAt come from venues.js (loaded only when the chart is).
(() => {
document.querySelectorAll('[data-fills-scroll]').forEach(box => Overseer.scrollFade(box));

if (!document.getElementById('wchart')) return;
const config = JSON.parse(document.getElementById('wallets-config').textContent);

// Wallet colour follows the wallet's VENUE, from the validated palette in
// venues.js — not its position in an array, so adding or removing a wallet
// never repaints the others, and the chart and the share meters (one keyed by
// label, one in config order) cannot disagree.
const WALLET_VENUE = Object.fromEntries(config.wallets.map(w => [w.label, w.venue]));

// Two wallets on one venue would share a hue, so they are separated by line
// style instead — colour still says "which venue", dash says "which wallet".
// (LightweightCharts.LineStyle: 0 solid, 2 dashed, 1 dotted, 3 large-dashed.)
const venueSeen = {};
const DASHES = [0, 2, 1, 3];
function walletStyle(label) {
  const venue = WALLET_VENUE[label];
  if (!(label in venueSeen)) {
    const n = Object.values(venueSeen).filter(v => v.venue === venue).length;
    venueSeen[label] = { venue, dash: DASHES[n % DASHES.length] };
  }
  return { color: colorFor(venue), lineStyle: venueSeen[label].dash };
}

const chart = LightweightCharts.createChart(document.getElementById('wchart'), chartOptions({
  leftPriceScale: { visible: true },
}));
const readout = legendReadout(chart, document.getElementById('wlegend'));
// Net flow is signed base units: one fixed precision for the whole axis.
const FLOW_FORMAT = { type: 'custom', minMove: 0.01, formatter: v => Fmt.number(v, 2, { sign: true }) };
let handles = [];
const walletFilters = Overseer.filterState('wallets');
const walletState = Overseer.pageState();
let walletRequest = 0;

async function loadWallets({ forceRefresh = false } = {}) {
  const request = ++walletRequest;
  const symbol = document.getElementById('wsym').value;
  const hours = document.getElementById('whours').value;
  // price context comes from hyperliquid, whose bare-coin symbols match the
  // wallet venues' convention (HL + lighter both call it just "BTC")
  const pxQs = new URLSearchParams({ exchange: 'hyperliquid', market_type: 'perp',
                                     symbol, interval: '1m', hours, limit: 5000 });
  walletState.loading('Loading wallet flows');
  try {
    const [flows, candles] = await Promise.all([
      Overseer.requestJSON(
        `/api/wallet-flows?${new URLSearchParams({ symbol, hours })}`,
        { forceRefresh },
      ),
      Overseer.requestJSON(`/api/candles?${pxQs}`, { forceRefresh }),
    ]);
    if (request !== walletRequest) return;
    document.getElementById('wchart-scope').textContent =
      `${symbol} · ${document.getElementById('whours').selectedOptions[0].textContent}`;
    handles.forEach(h => chart.removeSeries(h));
    handles = [];

    const entries = [];
    if (candles.length) {
      // Dim and dashed: context for the flow lines, never mistaken for one.
      const px = chart.addLineSeries({
        color: CHART_THEME.text, lineWidth: 1, lineStyle: 2, priceScaleId: 'right',
        priceFormat: CHART_PRICE_FORMAT, lastValueVisible: false, priceLineVisible: false,
      });
      const rows = candles.map(c => ({ time: c.time, value: c.close }));
      px.setData(rows);
      handles.push(px);
      entries.push({ label: `${symbol} price`, color: CHART_THEME.text, shape: 'dash', unit: 'price', rows });
    }
    Object.entries(flows).forEach(([label, pts]) => {
      if (!pts.length) return;
      let cum = 0;
      const style = walletStyle(label);
      const line = chart.addLineSeries({
        color: style.color, lineStyle: style.lineStyle, lineWidth: 2, priceScaleId: 'left',
        priceFormat: FLOW_FORMAT, lastValueVisible: false, priceLineVisible: false,
      });
      const rows = pts.map(p => ({ time: p.time, value: (cum += p.net) }));
      line.setData(rows);
      handles.push(line);
      // The swatch mirrors the line it stands for, so the legend still
      // identifies a series when two wallets share a venue hue.
      entries.push({ label: `${label.toUpperCase()} net flow ${labelFor(WALLET_VENUE[label])}`,
                     color: style.color, shape: style.lineStyle !== 0 ? 'dash' : 'line', unit: 'flow', rows });
    });
    // One precision per unit group: the flows share a formatter, price has its own.
    const formatters = Object.fromEntries(['flow', 'price'].map(unit => [unit,
      fixedFormatter(entries.filter(e => e.unit === unit).map(e => e.rows[e.rows.length - 1].value),
                     { sign: unit === 'flow' })]));
    readout.set(entries.map(e => ({
      label: e.label, color: e.color, shape: e.shape,
      text: (time) => {
        const row = time === null ? e.rows[e.rows.length - 1] : rowAt(e.rows, time);
        return formatters[e.unit](row ? row.value : null);
      },
    })));
    chart.timeScale().fitContent();
    const hasFlow = Object.values(flows).some(points => points.length);
    if (candles.length || hasFlow) walletState.ready();
    else walletState.empty(`No wallet flows or prices for ${symbol} in this window. ` +
      'Choose another asset or a longer window.');
  } catch (error) {
    if (request === walletRequest) walletState.error('Wallet flow', loadWallets);
  }
}

// ---- share meters: wallet 24h notional vs venue 24h volume ------------------
// Radial meter, not a 2-slice pie: one filled arc on a muted same-hue track,
// the share % as the figure. Entity hues match the flow lines above.
function meter(w, color) {
  const r = 34, c = 2 * Math.PI * r;
  const pct = w.share_pct;
  const dash = pct == null ? 0 : Math.min(pct, 100) / 100 * c;
  const shown = pct == null ? Fmt.DASH : Fmt.percent(pct, 1);
  return `
  <div class="kpi flex flex-col items-start gap-3 md:flex-row md:items-center">
    <div class="relative h-24 w-24 shrink-0" role="img" style="color:${color}"
         aria-label="${w.label} is ${pct == null ? 'unknown' : shown} of ${labelFor(w.venue)} 24h volume">
      <svg viewBox="0 0 84 84" class="h-full w-full" aria-hidden="true">
        <circle cx="42" cy="42" r="${r}" fill="none" stroke="currentColor" stroke-opacity="0.15" stroke-width="8"/>
        <circle cx="42" cy="42" r="${r}" fill="none" stroke="currentColor" stroke-width="8"
                stroke-linecap="round" stroke-dasharray="${dash} ${c}"
                transform="rotate(-90 42 42)"/>
      </svg>
      <span class="absolute inset-0 flex items-center justify-center font-mono text-data font-semibold
                   tabular-nums text-body">${shown}</span>
    </div>
    <div class="min-w-0">
      <div class="flex flex-wrap items-center gap-2">
        <span class="legend-swatch" data-shape="dot" style="--swatch:${color}"></span>
        <span class="min-w-0 break-words font-mono text-data font-medium text-body">${w.label.toUpperCase()}</span>
        <span class="font-mono text-label text-dim">${labelFor(w.venue)}</span>
      </div>
      <p class="mt-1 font-mono text-data text-dim">
        ${Fmt.usdCompact(w.wallet_notional_24h)} filled of ${Fmt.usdCompact(w.venue_notional_24h)} venue 24h volume</p>
      <p class="meta-line mt-1"><span>${Fmt.number(w.fills_24h ?? 0, 0)} fills</span></p>
    </div>
  </div>`;
}

async function loadShare({ forceRefresh = false } = {}) {
  try {
    const rows = await Overseer.requestJSON('/api/wallet-share', { forceRefresh });
    // colorFor(w.venue), not the row index — same source as the chart lines,
    // so a wallet is one colour everywhere on this page.
    const host = document.getElementById('wshare');
    host.className = rows.length ? 'kpi-grid kpi-grid-2' : '';
    host.innerHTML = rows.length
      ? rows.map(w => meter(w, colorFor(w.venue))).join('')
      : `<div class="summary-card empty-state">
           <p class="empty-state-title">No wallet fills in the last 24h.</p>
           <p class="empty-state-hint">Share appears once a tracked wallet trades.</p></div>`;
    walletState.ready('share');
  } catch (error) {
    walletState.error('Wallet share', loadShare, 'share');
  }
}

for (const id of ['wsym', 'whours']) {
  walletFilters.restoreSelect(document.getElementById(id));
}
document.getElementById('wsym').addEventListener('change', event => {
  walletFilters.set('wsym', event.target.value);
  loadWallets();
});
document.getElementById('whours').addEventListener('change', event => {
  walletFilters.set('whours', event.target.value);
  loadWallets();
});
Overseer.poll(loadWallets, 120);
Overseer.poll(loadShare, 120);
})();
