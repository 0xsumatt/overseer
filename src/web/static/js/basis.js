/* Spot–perpetual basis page. */
(() => {
  const PCT_AXIS = chartPercentFormat(2);
  // Token colour at partial opacity, so the bars sit behind the basis line.
  const translucent = (color, alpha) =>
    color.length === 7 ? color + Math.round(alpha * 255).toString(16).padStart(2, '0') : color;
  const FUNDING_UP = translucent(CHART_THEME.gain, 0.5);
  const FUNDING_DOWN = translucent(CHART_THEME.loss, 0.5);

  const chart = LightweightCharts.createChart(document.getElementById('chart'), chartOptions({
    leftPriceScale: { visible: true },
  }));
  const line = chart.addLineSeries({
    color: CHART_THEME.body, lineWidth: 2, priceFormat: PCT_AXIS, priceLineVisible: false,
  });
  const zero = chart.addLineSeries({ color: CHART_THEME.zero, lineWidth: 1, priceFormat: PCT_AXIS,
    priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  const funding = chart.addHistogramSeries({
    priceScaleId: 'left', priceFormat: PCT_AXIS, base: 0,
  });
  const basisFilters = Overseer.filterState('basis');
  const basisState = Overseer.pageState();
  let basisRequest = 0;

  let basisRows = [];
  let fundingRows = [];
  // Funding settles sparsely: the hovered value is the last settlement at or before the time.
  function fundingAt(time) {
    let found = null;
    for (const row of fundingRows) {
      if (row.time > time) break;
      found = row;
    }
    return found;
  }
  const readout = legendReadout(chart, document.getElementById('basis-legend'));
  readout.set([
    { label: 'Basis %, right axis', color: CHART_THEME.body, shape: 'line',
      text: time => {
        const row = time === null ? basisRows.at(-1) : rowAt(basisRows, time);
        return Fmt.signedPercent(row ? row.basis_pct : null, 2);
      } },
    { label: 'Funding APR, perp leg, left axis', color: CHART_THEME.gain, shape: 'bar',
      text: time => {
        const row = time === null ? fundingRows.at(-1) : fundingAt(time);
        return Fmt.signedPercent(row ? row.apr_pct : null, 2);
      } },
  ]);

  function setKpi(id, text, value = null) {
    const el = document.getElementById(id);
    el.textContent = text;
    el.classList.toggle('text-gain', value != null && value >= 0);
    el.classList.toggle('text-loss', value != null && value < 0);
  }

  function renderKpis(data, fdata, windowLabel) {
    const last = data.at(-1);
    if (last && last.basis_pct != null) {
      setKpi('kpi-basis', Fmt.signedPercent(last.basis_pct, 2), last.basis_pct);
      document.getElementById('kpi-basis-meta').textContent =
        `Perp vs spot · ${Fmt.time(last.time, { seconds: false })}`;
    } else {
      setKpi('kpi-basis', Fmt.DASH);
      document.getElementById('kpi-basis-meta').textContent = 'Perp vs spot';
    }
    document.getElementById('kpi-sum-meta').textContent = `Over ${windowLabel}`;
    if (fdata.length) {
      const cum = fdata.reduce((sum, f) => sum + f.rate, 0) * 100;
      const avg = fdata.reduce((sum, f) => sum + f.apr_pct, 0) / fdata.length;
      setKpi('kpi-apr', Fmt.signedPercent(avg, 2), avg);
      setKpi('kpi-sum', Fmt.signedPercent(cum, 2), cum);
      setKpi('kpi-count', Fmt.number(fdata.length, 0));
    } else {
      setKpi('kpi-apr', Fmt.DASH);
      setKpi('kpi-sum', Fmt.DASH);
      setKpi('kpi-count', Fmt.DASH);
    }
  }

  async function load({ forceRefresh = false } = {}) {
    const request = ++basisRequest;
    const [se, ss] = document.getElementById('spot').value.split('|');
    const [pe, ps] = document.getElementById('perp').value.split('|');
    const hoursSelect = document.getElementById('hours');
    const hours = hoursSelect.value;
    const windowLabel = hoursSelect.selectedOptions[0].textContent;
    const qs = new URLSearchParams({
      spot_exchange: se, spot_symbol: ss, perp_exchange: pe, perp_symbol: ps,
      interval: '1m', hours, limit: 10000,
    });
    const fqs = new URLSearchParams({ exchange: pe, symbol: ps, hours });
    document.getElementById('basis-pair').textContent = `${labelFor(se)} ${ss} vs ${labelFor(pe)} ${ps}`;
    basisState.loading('Loading basis and funding history');
    try {
      const [data, fdata] = await Promise.all([
        Overseer.requestJSON(`/api/basis?${qs}`, { forceRefresh }),
        Overseer.requestJSON(`/api/funding-history?${fqs}`, { forceRefresh }),
      ]);
      if (request !== basisRequest) return;
      basisRows = data;
      fundingRows = fdata;
      line.setData(data.map(d => ({ time: d.time, value: d.basis_pct })));
      zero.setData(data.map(d => ({ time: d.time, value: 0 })));
      funding.setData(fdata.map(f => ({
        time: f.time, value: f.apr_pct,
        color: f.apr_pct >= 0 ? FUNDING_UP : FUNDING_DOWN,
      })));
      renderKpis(data, fdata, windowLabel);
      readout.refresh();

      if (!data.length) {
        basisState.empty(`No overlapping prices for ${ss} spot and ${ps} perp in ${windowLabel}. ` +
          'Choose the same asset on both sides or a longer window.');
      } else {
        basisState.ready();
      }
      chart.timeScale().fitContent();
    } catch (error) {
      if (request === basisRequest) basisState.error('Basis data', load);
    }
  }
  for (const id of ['spot', 'perp', 'hours']) {
    const control = document.getElementById(id);
    basisFilters.restoreSelect(control);
    control.addEventListener('change', () => {
      basisFilters.set(id, control.value);
      load();
    });
  }
  Overseer.poll(load, 60);
})();
