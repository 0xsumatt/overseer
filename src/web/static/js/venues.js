/* Venue identity colours — the single source of truth.
 *
 * Was duplicated across dashboard.html, funding.html and flow.html, which is
 * how the three drifted. Colour follows the ENTITY: a venue must be the same
 * hue on every page, and a filter that hides some venues must never repaint
 * the ones that remain — so always iterate VENUE_ORDER, never the keys of
 * whatever the API happened to return.
 *
 * Chosen against the blue-black chart surface (#080B12). The palette stays
 * cool-dominant so it belongs to the terminal UI, while large lightness steps
 * keep neighbouring lines distinct instead of producing a same-brightness
 * rainbow. Binance is the one controlled warm identity.
 *
 * Core venue colors were checked for contrast and color-vision separation.
 * HIP-3 deployers use stable display identities of their own rather than
 * inheriting Hyperliquid's cyan, so overlapping markets remain distinguishable.
 *
 * Venue colors deliberately avoid the application's exact gain/loss tokens:
 * entity identity and market direction must not look like the same signal.
 * Brand-adjacent hues remain where useful—Hyperliquid teal, Bybit blue and
 * Binance gold—but chart legibility takes priority over logo fidelity.
 */
const VENUE_COLORS = {
  hyperliquid: '#2CECF5',   // branded ice cyan
  'hyperliquid:xyz': '#FF8A3D', // TradeXYZ warm orange
  'hyperliquid:io':  '#C084FC', // Entropy violet
  bybit:       '#00A0FF',   // terminal blue
  binance:     '#F8B600',   // controlled gold
  bullet:      '#7A53DB',   // violet
  rise:        '#DA58B2',   // rose-lilac
  extended:    '#008B76',   // branded deep green
  lighter:     '#BCAABA',   // silver-lilac
  bulk:        '#B4E04A',   // lime: clear of Lighter's silver and Binance's gold
};

/* Fixed iteration order. Never sort by value, count, or API key order — that
 * would make a venue's colour depend on its rank. */
const VENUE_ORDER = Object.keys(VENUE_COLORS);

/* Chart chrome follows the application tokens, read from the CSS custom
 * properties published in css/base.css, so canvases can never drift from the
 * DOM palette. The stylesheet is render-blocking, so the values exist here. */
const CHART_THEME = (() => {
  const css = getComputedStyle(document.documentElement);
  const token = name => css.getPropertyValue(`--color-${name}`).trim();
  return Object.freeze({
    text: token('dim'),
    body: token('body'),
    grid: token('line-soft'),
    crosshair: token('faint'),
    label: token('line'),
    border: token('line'),
    zero: token('line-strong'),
    accent: token('phosphor'),
    gain: token('gain'),
    loss: token('loss'),
    long: token('long'),
    short: token('short'),
    font: css.getPropertyValue('--font-mono').trim(),
  });
})();

/* Unknown venues (a symbol still in the DB for a venue dropped from config)
 * get a neutral grey rather than a generated hue. */
const colorFor = (v) => VENUE_COLORS[v] || CHART_THEME.text;

/* Display names never replace venue keys used for data, colours or selection. */
const labelFor = (venue) => VENUE_LABELS[venue] ||
  (venue.startsWith('hyperliquid:') ? `hip3:${venue.slice('hyperliquid:'.length)}` : venue);

/* Grouped, magnitude-aware numbers for axes, crosshair and last-value labels:
 * 107,000 rather than 107000.00, 0.1234 rather than 0.12; negatives use the
 * true minus like every other number on the site. */
const CHART_NUMBER_FORMATS = [
  [1000, new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 })],
  [10, new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 })],
  [1, new Intl.NumberFormat('en-US', { maximumFractionDigits: 3 })],
  [0, new Intl.NumberFormat('en-US', { maximumSignificantDigits: 4 })],
];
function formatChartNumber(value) {
  if (!Number.isFinite(value)) return '';
  const abs = Math.abs(value);
  return CHART_NUMBER_FORMATS.find(([floor]) => abs >= floor)[1].format(value).replace('-', Fmt.MINUS);
}

/* Values shown side by side (legends, readouts) share one precision, chosen
 * from the largest of them, so 104,404.0 sits next to 104,202.7 rather than
 * 104,404. Pass every value the set will show; returns a formatter that
 * prints a dash for anything missing. */
function fixedFormatter(values, { sign = false } = {}) {
  const magnitudes = values.filter(Number.isFinite).map(Math.abs);
  const top = magnitudes.length ? Math.max(...magnitudes) : 0;
  const digits = top >= 1000 ? 1 : top >= 10 ? 2 : top >= 1 ? 3 : 4;
  return value => Fmt.number(value, digits, { sign });
}

/* priceFormat for any series plotted in native units (prices, sizes). */
const CHART_PRICE_FORMAT = Object.freeze({
  type: 'custom', minMove: 0.00000001, formatter: formatChartNumber,
});

/* Fixed-precision percentage axis, e.g. chartPercentFormat(2) → "−0.99%". */
const chartPercentFormat = (digits = 2, signed = false) => Object.freeze({
  type: 'custom',
  minMove: 10 ** -digits,
  formatter: value => Fmt.percent(value, digits, { sign: signed }),
});

/* The row of a time-sorted series at exactly `time` (seconds), or null. */
function rowAt(rows, time) {
  let low = 0;
  let high = rows.length - 1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    if (rows[middle].time === time) return rows[middle];
    if (rows[middle].time < time) low = middle + 1;
    else high = middle - 1;
  }
  return null;
}

/* The one hover readout every line chart uses. The legend lists each series
 * with a value; while the crosshair is on the plot the values follow it and
 * the legend's time slot says when, and off the plot they return to the
 * latest. Pages describe the entries and how each reads its value:
 *
 *   const readout = legendReadout(chart, legendElement);
 *   readout.set([{ label: 'Bybit', color, shape: 'line',
 *                  text: (time, param) => '104,202.7' }, …]);
 *
 * text() gets time = null for the latest value, otherwise the hovered time
 * in seconds and the crosshair event (param.seriesData has each plotted
 * series' point there). Call readout.refresh() after data changes. */
function legendReadout(chart, legend, { empty = 'No series to show' } = {}) {
  let entries = [];
  let hovered = null;
  let lastParam = null;
  const stamp = document.createElement('span');
  stamp.className = 'legend-time';
  function paint() {
    stamp.textContent = hovered === null ? 'Latest' : Fmt.dateTime(hovered, { seconds: false });
    for (const entry of entries) entry.value.textContent = entry.text(hovered, lastParam);
  }
  chart.subscribeCrosshairMove(param => {
    const inside = param.point && param.point.x >= 0 && param.point.y >= 0 && param.time !== undefined;
    lastParam = inside ? param : null;
    const time = inside ? param.time : null;
    if (time === hovered && time === null) return;
    hovered = time;
    paint();
  });
  return {
    set(list) {
      entries = list.map(entry => {
        const item = document.createElement('span');
        item.className = 'legend-item';
        if (entry.title) item.title = entry.title;
        const swatch = document.createElement('span');
        swatch.className = 'legend-swatch';
        swatch.dataset.shape = entry.shape || 'line';
        swatch.style.setProperty('--swatch', entry.color);
        const value = document.createElement('span');
        value.className = 'legend-value';
        item.append(swatch, document.createTextNode(entry.label), value);
        return { ...entry, item, value };
      });
      const items = entries.map(entry => entry.item);
      if (!items.length) {
        const none = document.createElement('span');
        none.className = 'legend-item text-faint';
        none.textContent = empty;
        items.push(none);
      }
      legend.replaceChildren(...items, stamp);
      paint();
    },
    refresh: paint,
  };
}

function mergeChartOptions(base, overrides) {
  const merged = { ...base };
  for (const [key, value] of Object.entries(overrides)) {
    const current = merged[key];
    merged[key] = value && typeof value === 'object' && !Array.isArray(value)
        && current && typeof current === 'object' && !Array.isArray(current)
      ? mergeChartOptions(current, value)
      : value;
  }
  return merged;
}

/* Every chart starts from the same chrome; pass only what differs. The
 * TradingView attribution is shown once in the page footer instead of a logo
 * over each plot. Charts size themselves to their container (autoSize), so
 * the container must have a definite height. A tiny minimum bar spacing lets
 * fitContent() show the whole selected window on narrow screens. */
function chartOptions(overrides = {}) {
  return mergeChartOptions({
    autoSize: true,
    layout: {
      background: { color: 'transparent' },
      textColor: CHART_THEME.text,
      fontFamily: CHART_THEME.font,
      fontSize: 12,
      attributionLogo: false,
    },
    grid: {
      vertLines: { color: CHART_THEME.grid },
      horzLines: { color: CHART_THEME.grid },
    },
    crosshair: {
      vertLine: { color: CHART_THEME.crosshair, labelBackgroundColor: CHART_THEME.label },
      horzLine: { color: CHART_THEME.crosshair, labelBackgroundColor: CHART_THEME.label },
    },
    timeScale: {
      timeVisible: true, secondsVisible: false, borderColor: CHART_THEME.border, minBarSpacing: 0.01,
    },
    rightPriceScale: { borderColor: CHART_THEME.border },
    leftPriceScale: { borderColor: CHART_THEME.border },
  }, overrides);
}
