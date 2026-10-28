/* Depth heatmap: stored order-book snapshots drawn as resting size against
 * time. Each column is one stored book: blue bids below its mid, purple asks
 * above, brighter for more size per bucket, over a faint band for the part of
 * the book the snapshot showed. "Around mid" plots distance from the mid in
 * bps, so even a narrow book fills the chart; "Price" plots price levels, so
 * walls at fixed prices line up across time. The heatmap is a background
 * primitive on a line series (the mid, or zero), so axes, zoom and crosshair
 * are the chart's own. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const configNode = $('depth-config');
  if (!configNode) return;
  const config = JSON.parse(configNode.textContent);
  if (!config.assets?.length) return;

  const assetSelect = $('depth-asset');
  const venueSelect = $('depth-venue');
  const windowSelect = $('depth-window');
  const viewButtons = [...document.querySelectorAll('[data-depth-view]')];
  const scope = $('depth-scope');
  const scaleNote = $('depth-scale');
  const readout = $('depth-readout');
  const chartNode = $('depth-chart');
  const pageState = Overseer.pageState();
  const filters = Overseer.filterState('depth');
  const windowBps = Number(config.window_bps) || 100;
  const assetsById = new Map(config.assets.map(asset => [asset.id, asset]));
  // Fills are batched into this many strengths, so a paint stays cheap.
  const STRENGTH_STEPS = 12;

  const bpsText = value => Number(value).toLocaleString('en-US', {maximumFractionDigits: 1});
  const stepText = seconds => (seconds >= 3600 ? `one snapshot per ${seconds / 3600} h` : `one snapshot per ${seconds / 60} min`);
  const visibleBps = reach => Math.min(Number(reach) || 0, windowBps);

  // Each view maps a signed distance from a column's mid, in bps, onto the
  // chart's vertical axis, and says what the line series plots.
  const VIEWS = {
    mid: {
      value: (_column, bps) => bps,
      line: () => 0,
      priceFormat: {
        type: 'custom', minMove: 0.1,
        formatter: value => {
          // Sign the rounded value, so a hair below zero reads "0 bps", not "−0 bps".
          const rounded = Math.round(value * 10) / 10;
          return `${rounded > 0 ? '+' : rounded < 0 ? '−' : ''}${bpsText(Math.abs(rounded))} bps`;
        },
      },
    },
    price: {
      value: (column, bps) => column.mid * (1 + bps / 10000),
      line: column => column.mid,
      priceFormat: CHART_PRICE_FORMAT,
    },
  };

  function cellUsd(column, size, index, below) {
    const middle = (index + 0.5) * column.bucket_bps * (below ? -1 : 1);
    return size * column.mid * (1 + middle / 10000) * (column.usd_rate ?? 1);
  }
  // Full colour at the 95th percentile of this window's cells, so one wall
  // cannot wash out the rest of the book.
  function strengthScale(columns) {
    const values = [];
    for (const column of columns) {
      column.bids.forEach((size, index) => { if (size) values.push(cellUsd(column, size, index, true)); });
      column.asks.forEach((size, index) => { if (size) values.push(cellUsd(column, size, index, false)); });
    }
    if (!values.length) return 1;
    values.sort((a, b) => a - b);
    return values[Math.min(values.length - 1, Math.floor(values.length * 0.95))];
  }

  class DepthHeatmap {
    constructor() {
      this.slots = [];
      this.scale = 1;
      this.axis = VIEWS.mid;
      this.chart = null;
      this.series = null;
      this.requestUpdate = null;
      const heatmap = this;
      this.paneView = {
        zOrder: () => 'bottom',
        renderer: () => ({draw() {}, drawBackground: target => heatmap.paint(target)}),
      };
    }
    attached({chart, series, requestUpdate}) {
      this.chart = chart;
      this.series = series;
      this.requestUpdate = requestUpdate;
    }
    detached() {
      this.chart = null;
      this.series = null;
      this.requestUpdate = null;
    }
    updateAllViews() {}
    paneViews() {
      return [this.paneView];
    }
    setData(slots, scale, axis) {
      this.slots = slots;
      this.scale = scale;
      this.axis = axis;
      this.requestUpdate?.();
    }
    // Keep the part of each book that was shown in view, not the empty
    // window beyond it.
    autoscaleInfo(from, to) {
      let low = Infinity;
      let high = -Infinity;
      for (let i = Math.max(0, Math.floor(from)); i <= Math.min(this.slots.length - 1, Math.ceil(to)); i++) {
        const column = this.slots[i].column;
        if (!column) continue;
        low = Math.min(low, this.axis.value(column, -visibleBps(column.bid_reach_bps)));
        high = Math.max(high, this.axis.value(column, visibleBps(column.ask_reach_bps)));
      }
      return low < high ? {priceRange: {minValue: low, maxValue: high}} : null;
    }
    paint(target) {
      const {chart, series, slots, axis} = this;
      if (!chart || !series || !slots.length) return;
      const timeScale = chart.timeScale();
      const range = timeScale.getVisibleLogicalRange();
      const x0 = timeScale.logicalToCoordinate(0);
      const x1 = timeScale.logicalToCoordinate(1);
      const sample = slots.find(slot => slot.column)?.column;
      if (!range || x0 === null || x1 === null || !sample) return;
      // Both views are linear in bps, so two reference values place every edge.
      const a = axis.value(sample, 0);
      const b = axis.value(sample, 100);
      const yA = series.priceToCoordinate(a);
      const yB = series.priceToCoordinate(b);
      if (yA === null || yB === null) return;
      const slope = (yB - yA) / (b - a);
      const y = (column, bps) => yA + (axis.value(column, bps) - a) * slope;
      const spacing = Math.max(1, x1 - x0);
      target.useBitmapCoordinateSpace(({context: ctx, horizontalPixelRatio: hr, verticalPixelRatio: vr}) => {
        const band = [];
        const cells = {
          bids: Array.from({length: STRENGTH_STEPS}, () => []),
          asks: Array.from({length: STRENGTH_STEPS}, () => []),
        };
        const rect = (list, left, width, top, bottom) => {
          const start = Math.floor(Math.min(top, bottom) * vr);
          list.push(left, start, width, Math.max(1, Math.ceil(Math.max(top, bottom) * vr) - start));
        };
        const last = Math.min(slots.length - 1, Math.ceil(range.to) + 1);
        for (let i = Math.max(0, Math.floor(range.from) - 1); i <= last; i++) {
          const column = slots[i].column;
          const x = column ? timeScale.logicalToCoordinate(i) : null;
          if (x === null) continue;
          const left = Math.round((x - spacing / 2) * hr);
          const width = Math.max(1, Math.round((x + spacing / 2) * hr) - left);
          rect(band, left, width, y(column, visibleBps(column.ask_reach_bps)), y(column, -visibleBps(column.bid_reach_bps)));
          for (const [side, sign] of [['bids', -1], ['asks', 1]]) {
            // The outermost bucket ends where the book did, not at its own edge.
            const reach = visibleBps(sign < 0 ? column.bid_reach_bps : column.ask_reach_bps);
            column[side].forEach((size, index) => {
              if (!size) return;
              const strength = Math.min(1, Math.sqrt(cellUsd(column, size, index, sign < 0) / this.scale));
              rect(cells[side][Math.min(STRENGTH_STEPS - 1, Math.floor(strength * STRENGTH_STEPS))], left, width,
                   y(column, sign * index * column.bucket_bps),
                   y(column, sign * Math.min((index + 1) * column.bucket_bps, reach)));
            });
          }
        }
        ctx.save();
        ctx.fillStyle = CHART_THEME.border;
        ctx.globalAlpha = 0.35;
        for (let k = 0; k < band.length; k += 4) ctx.fillRect(band[k], band[k + 1], band[k + 2], band[k + 3]);
        for (const [side, color] of [['bids', CHART_THEME.long], ['asks', CHART_THEME.short]]) {
          ctx.fillStyle = color;
          cells[side].forEach((list, step) => {
            ctx.globalAlpha = 0.16 + 0.84 * (step + 1) / STRENGTH_STEPS;
            for (let k = 0; k < list.length; k += 4) ctx.fillRect(list[k], list[k + 1], list[k + 2], list[k + 3]);
          });
        }
        ctx.restore();
      });
    }
  }

  const chart = LightweightCharts.createChart(chartNode, chartOptions({
    // A free crosshair: the readout needs the level under the pointer, not
    // the line it would snap to.
    crosshair: {mode: LightweightCharts.CrosshairMode.Normal},
    rightPriceScale: {scaleMargins: {top: 0.08, bottom: 0.08}},
  }));
  const lineSeries = chart.addLineSeries({
    color: CHART_THEME.text, lineWidth: 1, priceFormat: VIEWS.mid.priceFormat,
    lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false,
  });
  const heatmap = new DepthHeatmap();
  lineSeries.attachPrimitive(heatmap);

  let loaded = null;
  let latest = null;
  let slots = [];
  let slotsByTime = new Map();
  let scale = 1;
  let view = filters.get('view') === 'price' ? 'price' : 'mid';
  let request = 0;

  function summary(column) {
    return `${Fmt.dateTime(column.captured_at)} · mid ${formatChartNumber(column.mid)} ${column.quote_currency}`
      + ` · book shown to −${bpsText(column.bid_reach_bps)} / +${bpsText(column.ask_reach_bps)} bps`
      + ` · ${Fmt.number(column.bid_levels, 0)} bid and ${Fmt.number(column.ask_levels, 0)} ask levels`;
  }
  function showLatest() {
    readout.textContent = latest ? `Latest ${summary(latest)}` : '';
  }
  // Redraws the current data in the current view; the time range stays put.
  function draw() {
    const axis = VIEWS[view];
    for (const button of viewButtons) button.setAttribute('aria-pressed', String(button.dataset.depthView === view));
    lineSeries.applyOptions({priceFormat: axis.priceFormat});
    lineSeries.setData(slots.map(slot => (slot.column ? {time: slot.time, value: axis.line(slot.column)} : {time: slot.time})));
    heatmap.setData(slots, scale, axis);
  }
  function render(data) {
    loaded = data;
    const columns = data.columns || [];
    scope.textContent = `${data.asset} on ${labelFor(data.identity)} · ${stepText(data.step_seconds)}`;
    slots = [];
    if (columns.length) {
      const byTime = new Map(columns.map(column => [column.time, column]));
      // Every slot from the first column to the last, so a gap in storage
      // stays a visible gap rather than closing up.
      for (let time = columns[0].time; time <= columns[columns.length - 1].time; time += data.step_seconds) {
        slots.push({time, column: byTime.get(time) || null});
      }
    }
    slotsByTime = new Map(slots.map(slot => [slot.time, slot]));
    scale = strengthScale(columns);
    draw();
    chart.timeScale().fitContent();
    latest = columns[columns.length - 1] || null;
    scaleNote.textContent = latest ? `Full colour at ${Fmt.usdCompact(scale)}+ per 1 bps bucket` : '';
    chartNode.setAttribute('aria-label', latest
      ? `Order-book depth for ${data.asset} on ${labelFor(data.identity)}, ${columns.length} snapshots. Latest: ${summary(latest)}.`
      : `No order-book depth stored for ${data.asset} on ${labelFor(data.identity)} in this window.`);
    if (latest) pageState.ready();
    else pageState.empty(`No depth stored for ${data.asset} on ${labelFor(data.identity)} in this window. Snapshots are saved every 5 minutes while the scheduler runs.`);
    showLatest();
  }
  async function load({forceRefresh = false} = {}) {
    const current = ++request;
    const params = new URLSearchParams({asset: assetSelect.value, venue: venueSelect.value, hours: windowSelect.value});
    pageState.loading('Loading depth');
    try {
      const data = await Overseer.requestJSON(`${config.endpoint}?${params}`, {forceRefresh});
      if (current === request) render(data);
    } catch (_) {
      if (current === request) pageState.error('Depth', () => load({forceRefresh: true}));
    }
  }

  chart.subscribeCrosshairMove(param => {
    const slot = param.point && param.time !== undefined ? slotsByTime.get(param.time) : null;
    if (!slot) {
      showLatest();
      return;
    }
    const column = slot.column;
    if (!column) {
      readout.textContent = `${Fmt.dateTime(slot.time)} · no snapshot stored`;
      return;
    }
    const value = lineSeries.coordinateToPrice(param.point.y);
    if (value === null) {
      showLatest();
      return;
    }
    const offset = view === 'mid' ? value : (value - column.mid) / column.mid * 10000;
    const below = offset < 0;
    const distance = Math.abs(offset);
    const side = below ? 'bids' : 'asks';
    const reach = Number(below ? column.bid_reach_bps : column.ask_reach_bps);
    const price = column.mid * (1 + offset / 10000);
    const at = `${Fmt.dateTime(column.captured_at)} · ${formatChartNumber(price)} ${column.quote_currency}`;
    if (distance >= windowBps) {
      readout.textContent = `${at}: outside the stored ±${windowBps} bps window`;
      return;
    }
    if (distance > reach) {
      readout.textContent = `${at}: beyond the book shown; its ${side} reached ${bpsText(reach)} bps from the mid`;
      return;
    }
    const index = Math.floor(distance / column.bucket_bps);
    const size = column[side][index] || 0;
    const where = `${bpsText(index * column.bucket_bps)}–${bpsText((index + 1) * column.bucket_bps)} bps ${below ? 'below' : 'above'} the mid`;
    readout.textContent = size
      ? `${at}: ${Fmt.number(size, size >= 1000 ? 0 : 4)} ${loaded.asset} of ${side} ${where} (${Fmt.usdCompact(cellUsd(column, size, index, below))})`
      : `${at}: nothing resting ${where}`;
  });

  function fillVenues() {
    const venues = assetsById.get(assetSelect.value)?.venues || [];
    venueSelect.replaceChildren(...venues.map(venue => {
      const option = new Option(labelFor(venue.identity), venue.id);
      if (venue.label) option.title = venue.label;
      return option;
    }));
    filters.restoreSelect(venueSelect, 'venue');
  }
  filters.restoreSelect(assetSelect, 'asset');
  filters.restoreSelect(windowSelect, 'hours');
  fillVenues();
  assetSelect.addEventListener('change', () => {
    filters.set('asset', assetSelect.value);
    fillVenues();
    load();
  });
  venueSelect.addEventListener('change', () => {
    filters.set('venue', venueSelect.value);
    load();
  });
  windowSelect.addEventListener('change', () => {
    filters.set('hours', windowSelect.value);
    load();
  });
  for (const button of viewButtons) {
    button.addEventListener('click', () => {
      view = button.dataset.depthView;
      filters.set('view', view);
      draw();
      showLatest();
    });
  }
  // A new snapshot lands every five minutes.
  Overseer.poll(load, 300);
})();
