/* Margin calculator: one live calculation per settings change. The server
 * captures books and margin rules for every venue; this page ranks and draws
 * them. One request runs at a time; a change made meanwhile is calculated
 * once it settles. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const configNode = $('margin-config');
  if (!configNode) return;
  const config = JSON.parse(configNode.textContent);
  const schedules = config.fee_schedules || {};
  const holdLabels = Object.fromEntries((config.holds || []).map(([hours, text]) => [String(hours), text]));
  const inputs = {
    asset: $('margin-asset'), direction: $('margin-direction'), size: $('margin-size'), goal: $('margin-goal'),
    move: $('margin-move'), hold: $('margin-hold'), rate: $('margin-rate'),
    pairAsset: $('margin-pair-asset'), pairDirection: $('margin-pair-direction'), pairSize: $('margin-pair-size'),
  };
  const status = $('margin-status');
  const statusState = $('margin-status-state');
  const tbody = $('margin-rows');
  const table = $('margin-table');
  const refresh = $('margin-refresh');
  const pageState = Overseer.pageState();
  const filters = Overseer.filterState('margin');
  const feeChoices = Object.create(null);
  // Phones fold both toolbars behind one summary line.
  const syncFilters = Overseer.filtersFold($('margin-filters'), () => {
    const leg = (direction, asset, size) => {
      const value = amount(size);
      return `${direction[0].toUpperCase()}${direction.slice(1)} ${asset}${value === null ? '' : ` ${Fmt.usdCompact(value)}`}`;
    };
    const legs = [leg(inputs.direction.value, inputs.asset.value, inputs.size.value)];
    if (pairActive()) legs.push(leg(inputs.pairDirection.value, inputs.pairAsset.value, inputs.pairSize.value));
    const hours = Number(inputs.hold.value);
    return `${legs.join(' + ')} · survive ${inputs.move.value.trim()}% · ${hours ? `hold ${holdLabels[inputs.hold.value]}` : 'no hold'}`;
  });
  let controller = null;
  let queued = false;
  let generation = 0;

  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  };
  const supplied = value => value !== null && value !== undefined && value !== '';
  // Capital and margin amounts in whole dollars; costs (digits = 2) in cents.
  const usd = (value, digits = 0) => Fmt.usd(value, digits);
  // Costs read as amounts paid. Money the position receives (funding, or a
  // total that funding outweighs) says "received" instead of showing as a
  // negative cost.
  function flow(value) {
    const numeric = Number(value);
    if (!supplied(value) || !Number.isFinite(numeric)) return {text: Fmt.DASH, received: false, zero: true};
    return {text: Fmt.usd(Math.abs(numeric), 2), received: numeric < 0, zero: Math.abs(numeric) < 0.005};
  }
  // The server sends fractions; Fmt.percent takes percentages.
  const percent = (fraction, digits = 2) => (supplied(fraction) ? Fmt.percent(Number(fraction) * 100, digits) : Fmt.DASH);
  // Margin rates without padding zeros: 1.2%, 0.33%, 2%.
  function rate(fraction) {
    const numeric = Number(fraction) * 100;
    return supplied(fraction) && Number.isFinite(numeric) ? `${numeric.toLocaleString('en-US', {maximumFractionDigits: 3})}%` : Fmt.DASH;
  }
  const feeText = takerBps => (supplied(takerBps) ? Fmt.feeBps(takerBps) : 'unknown, not zero');
  const statusLabels = {
    fee_unknown: 'Fee unknown', funding_unknown: 'Funding unknown', insufficient_depth: 'Snapshot too shallow',
    order_limit: 'Above venue limit', margin_unavailable: 'Margin rules unavailable',
    above_margin_limit: 'Above venue position limit',
    stale: 'Quote expired', fx_missing: 'USD conversion unavailable', invalid_book: 'Book unavailable',
    invalid_timestamp: 'Age unknown', no_reference: 'No USD reference', unavailable: 'Unavailable',
  };
  const label = value => statusLabels[value] || 'Unavailable';

  function pairActive() {
    return Boolean(inputs.pairAsset.value);
  }
  function syncControls() {
    const capital = inputs.goal.value === 'capital';
    inputs.pairDirection.disabled = !pairActive();
    inputs.pairSize.disabled = !pairActive();
    for (const option of inputs.pairAsset.options) option.disabled = option.value === inputs.asset.value;
    if (inputs.pairAsset.value === inputs.asset.value) inputs.pairAsset.value = '';
    $('margin-best-help').textContent = capital
      ? 'Least capital needed to survive your move'
      : 'Lowest total cost to open, hold and close';
    let custom = 0;
    for (const [venue, schedule] of Object.entries(schedules)) {
      if (feeChoices[venue] && feeChoices[venue] !== schedule.default_tier) custom++;
    }
    $('margin-fee-summary').textContent = custom ? `${custom} modified ${custom === 1 ? 'tier' : 'tiers'}` : 'Base tiers';
    syncFilters();
  }
  function buildFeeControls() {
    const container = $('margin-fee-controls');
    for (const [venue, schedule] of Object.entries(schedules)) {
      const field = node('label', null, 'field');
      field.append(node('span', schedule.label, 'field-label'));
      const shell = node('span', null, 'select-shell');
      const select = node('select', null, 'select select-polished');
      select.setAttribute('aria-label', `${schedule.label} fee tier`);
      for (const tier of schedule.tiers || []) {
        const option = node('option', `${tier.label} · ${feeText(tier.taker_bps)}`);
        option.value = tier.id;
        select.append(option);
      }
      feeChoices[venue] = schedule.default_tier;
      select.value = schedule.default_tier;
      select.addEventListener('change', () => {
        feeChoices[venue] = select.value;
        syncControls();
        calculate();
      });
      shell.append(select);
      field.append(shell);
      container.append(field);
    }
  }

  // Plain digits only: "100,000" and "$100k" are accepted as typed.
  function amount(text) {
    const cleaned = String(text).trim().toLowerCase().replace(/[$,\s]/g, '');
    const match = cleaned.match(/^(\d+(?:\.\d+)?)([km]?)$/);
    if (!match) return null;
    const value = Number(match[1]) * (match[2] === 'k' ? 1e3 : match[2] === 'm' ? 1e6 : 1);
    return Number.isFinite(value) ? value : null;
  }
  class InputError extends Error {
    constructor(input, message) {
      super(message);
      this.input = input;
    }
  }
  function requestBody() {
    const size = amount(inputs.size.value);
    const move = amount(inputs.move.value);
    const rate = inputs.rate.value.trim() ? amount(inputs.rate.value) : 0;
    if (size === null) throw new InputError(inputs.size, 'Enter the size in USD, for example 100000 or 100k.');
    if (move === null || move < 0.1 || move > 100) {
      throw new InputError(inputs.move, 'Enter the move to survive as a percentage from 0.1 to 100, for example 10.');
    }
    if (rate === null) throw new InputError(inputs.rate, 'Enter the capital rate as a yearly percentage, for example 5.');
    const body = {
      position: {asset: inputs.asset.value, direction: inputs.direction.value, size: String(size)},
      move: String(move), hold_hours: Number(inputs.hold.value), rate: String(rate), rank: inputs.goal.value,
      fees: Object.fromEntries(Object.entries(feeChoices).filter(([venue, tier]) => tier !== schedules[venue]?.default_tier)),
    };
    if (pairActive()) {
      const pairSize = amount(inputs.pairSize.value);
      if (pairSize === null) throw new InputError(inputs.pairSize, 'Enter the second position\'s size in USD.');
      body.pair = {asset: inputs.pairAsset.value, direction: inputs.pairDirection.value, size: String(pairSize)};
    }
    return body;
  }

  function describe(body) {
    const legs = [body.position, body.pair].filter(Boolean)
      .map(leg => `${leg.direction} ${leg.asset} ${usd(leg.size)}`).join(' + ');
    const survive = body.pair ? `survive ${body.move}% against each position` : `survive a ${body.move}% move`;
    const hold = Number(body.hold_hours) ? `hold ${holdLabels[String(body.hold_hours)]}` : 'no hold';
    return `${legs} · ${survive} · ${hold}`;
  }
  function priceText(value) {
    return Number(value).toLocaleString('en-US', {maximumSignificantDigits: 6});
  }
  // Status bar: a short value, plus an optional state with a tone. Phones drop
  // the lead ("Captured ") and a routine state ("7/8 ranked", the settings
  // while calculating) to keep the bar on one line; problems always show.
  function setStatus(state, value, detail = '', tone = '', lead = '') {
    status.dataset.state = state;
    status.replaceChildren(...(lead ? [node('span', lead, 'status-bar-wide')] : []), value);
    if (!statusState) return;
    statusState.hidden = !detail;
    statusState.classList.toggle('status-bar-wide', state === 'ready' || state === 'loading');
    statusState.querySelector('[data-status-text]').textContent = detail;
    if (tone) statusState.dataset.tone = tone;
    else delete statusState.dataset.tone;
  }
  function setBest(name, text, meta = '') {
    $('margin-best').querySelector(`[data-best="${name}"]`).textContent = text;
    const metaNode = $('margin-best').querySelector(`[data-best="${name}-meta"]`);
    if (metaNode) metaNode.textContent = meta;
  }
  // Capital for the pair against each position held on its own: BULK's
  // portfolio margin saves (hedges) or adds (large legs at low leverage);
  // elsewhere a saving appears only when one leg's leverage cap leaves spare
  // collateral the other can use. Empty when they match.
  function offsetText(combined, sentence = false) {
    const offset = Number(combined.offset_usd);
    const alone = Number(combined.alone_usd);
    if (!alone || Math.abs(offset) < 0.005) return '';
    if (sentence) return `${usd(Math.abs(offset))} ${offset > 0 ? 'less' : 'more'}`;
    const share = Math.abs(offset) / alone;
    return `${offset > 0 ? '−' : '+'}${percent(share, share < 0.01 ? 1 : 0)} vs legs alone`;
  }
  function renderBest(data, pair) {
    const winners = data.venues.filter(row => row.best);
    const best = winners[0];
    $('margin-best').querySelector('[data-best="risk-label"]').textContent = pair ? 'Versus legs alone' : 'Liquidation';
    if (!best) {
      for (const name of ['venue', 'cost', 'margin', 'risk']) setBest(name, '—', name === 'venue' ? 'No venue can take this position as set' : '');
      return;
    }
    const caps = `${best.legs.map(leg => Fmt.leverageCap(leg.max_leverage)).join(' / ')} max at this size`;
    setBest('venue', winners.map(row => labelFor(row.identity)).join(' / '), best.margin?.portfolio ? `Portfolio margin · ${caps}` : caps);
    const net = flow(best.costs.total_usd);
    $('margin-best').querySelector('[data-best="cost-label"]').textContent = net.received ? 'Net received' : 'Total cost';
    setBest('cost', net.text, net.received
      ? `${Fmt.bps(Math.abs(Number(best.costs.total_bps)))} of size · funding received outweighs trading and capital costs`
      : `${Fmt.bps(best.costs.total_bps)} of size · trading, funding and capital`);
    const combined = best.combined;
    setBest('margin', usd(combined.capital_usd),
      `${Fmt.leverage(combined.leverage)} leverage${combined.capped ? ' · set by the venue\'s leverage cap' : ''}`);
    if (pair) {
      setBest('risk', offsetText(combined, true) || 'Same', `each position on its own needs ${usd(combined.alone_usd)}`);
    } else if (supplied(combined.liquidation_move)) {
      setBest('risk', percent(combined.liquidation_move), `adverse move to ${priceText(combined.liquidation_price)} with the capital needed posted`);
    } else {
      setBest('risk', 'None', 'a fully funded long cannot be liquidated');
    }
  }
  function detailList(row, pair) {
    const list = node('dl', null, 'margin-detail-list');
    const line = (name, value) => list.append(node('dt', name), node('dd', value));
    for (const leg of row.legs) {
      // Sentence case: "Max leverage", or "Long BTC · max leverage" for a pair.
      const prefix = pair ? `${leg.direction} ${leg.asset} · ` : '';
      const labelText = text => {
        const whole = prefix + text;
        return whole[0].toUpperCase() + whole.slice(1);
      };
      line(labelText('max leverage'), `${Fmt.leverageCap(leg.max_leverage)} at this size`);
      line(labelText('maintenance now'), `${usd(leg.maintenance_usd)} · ${rate(leg.maintenance_rate)} of size`);
      line(labelText('loss at the move'), usd(leg.loss_usd));
      line(labelText('maintenance after the move'), usd(leg.moved_maintenance_usd));
      if (pair) line(labelText('capital on its own'), usd(leg.alone_usd));
      line(labelText('trading cost'), leg.estimate ? `${usd(leg.estimate.trading_cost_usd, 2)} · ${Fmt.bps(leg.estimate.trading_cost_bps)}` : Fmt.DASH);
      if (leg.funding) {
        const amount = flow(leg.estimate?.funding_usd);
        line(labelText('funding'), leg.funding.unavailable ? `${leg.funding.unavailable}, not ranked`
          : amount.zero ? amount.text
            : `${amount.text} ${amount.received ? 'received' : 'paid'} · ${Fmt.bps(Math.abs(Number(leg.funding.cost_bps)))}`);
      }
    }
    const combined = row.combined;
    if (combined) {
      const parts = Number(combined.loss_usd) + Number(combined.moved_maintenance_usd);
      line('Capital needed', combined.capped
        ? `${usd(combined.capital_usd)}: the venue's minimum at its maximum leverage for this size`
        : Math.abs(Number(combined.capital_usd) - parts) < 0.02
          ? `${usd(combined.capital_usd)}: loss ${usd(combined.loss_usd)} plus maintenance after the move ${usd(combined.moved_maintenance_usd)}`
          : `${usd(combined.capital_usd)}: the most needed on the way, just before a lower bracket's maintenance applies`);
      line('Leverage', `${Fmt.leverage(combined.leverage)}: size ÷ capital needed`);
      if (pair) {
        line('Maintenance now', `${usd(combined.maintenance_usd)}${row.margin?.portfolio ? ', after portfolio offsets' : ''}`);
        const offset = offsetText(combined);
        line('Each position on its own', `${usd(combined.alone_usd)}${offset ? ` (pair ${offset})` : ''}`);
      } else {
        line('Liquidation', supplied(combined.liquidation_price)
          ? `${percent(combined.liquidation_move)} away, at ${priceText(combined.liquidation_price)} ${row.legs[0].quote_currency}`
          : 'None: a fully funded long');
      }
    }
    line('Fee tier', `${row.fee.tier_label} · ${feeText(row.fee.taker_bps)}`);
    if (row.margin) line('Margin rules', row.margin.source);
    const body = node('div', null, 'margin-detail-body');
    body.append(list);
    for (const note of row.margin?.notes || []) body.append(node('p', note, 'margin-detail-note'));
    if (row.error) body.append(node('p', row.error, 'margin-detail-note'));
    return body;
  }
  function renderRows(data, pair) {
    const key = row => Number(data.rank === 'cost' ? row.costs?.total_usd : row.combined?.capital_usd);
    const rows = [...data.venues].sort((a, b) => {
      const ranked = (b.status === 'ok') - (a.status === 'ok');
      return ranked || (a.status === 'ok' ? key(a) - key(b) : 0);
    });
    tbody.replaceChildren(...rows.map(row => {
      const tr = node('tr');
      tr.dataset.venue = row.venue;
      tr.dataset.best = String(row.best);
      const venue = node('th', null, 'sticky-col margin-venue-cell');
      venue.scope = 'row';
      const heading = node('div', null, 'venue-heading');
      const dot = node('span', null, 'status-dot');
      dot.style.color = colorFor(row.identity);
      dot.setAttribute('aria-hidden', 'true');
      const name = labelFor(row.identity);
      const nameNode = node('span', name, 'venue-name');
      if (row.label) nameNode.title = row.label;
      heading.append(dot, nameNode);
      // Line two: notes on the left, Details on the right; open details
      // drop below both.
      const meta = node('div', null, 'venue-meta');
      const notes = node('div', null, 'margin-venue-notes');
      // The row is the winner, so its mark sits with the venue rather than
      // widening the total column.
      if (row.best) notes.append(node('span', 'best', 'margin-best-mark'));
      if (row.margin?.portfolio) notes.append(node('span', 'portfolio margin', 'margin-portfolio-tag'));
      if (row.status !== 'ok') notes.append(node('span', label(row.status), 'margin-row-state'));
      const details = node('details');
      const summary = node('summary', 'Details', 'bracket-btn');
      summary.setAttribute('aria-label', `${name} margin and cost details`);
      details.append(summary, detailList(row, pair));
      meta.append(notes, details);
      venue.append(heading, meta);
      const combined = row.combined;
      const cells = [
        null,
        combined ? usd(combined.capital_usd) : Fmt.DASH,
        combined ? Fmt.leverage(combined.leverage) : Fmt.DASH,
        combined ? usd(combined.maintenance_usd) : Fmt.DASH,
        row.costs ? usd(row.costs.trading_usd, 2) : Fmt.DASH,
        row.costs ? null : Fmt.DASH,
        row.costs ? usd(row.costs.capital_usd, 2) : Fmt.DASH,
      ].map(text => node('td', text, 'num'));
      // A pair's caps stack one per line, in the legend's order.
      cells[0].replaceChildren(...row.legs.map(leg => node('span', Fmt.leverageCap(leg.max_leverage), 'block')));
      if (combined?.capped) {
        const note = node('span', 'leverage cap', 'margin-flow-note');
        note.dataset.tone = 'warning';
        cells[1].append(note);
      }
      const offset = pair && combined ? offsetText(combined) : '';
      if (offset) {
        const note = node('span', offset, 'margin-offset');
        note.dataset.tone = Number(combined.offset_usd) > 0 ? 'gain' : 'loss';
        cells[1].append(note);
      }
      if (row.costs) {
        const funding = flow(row.costs.funding_usd);
        cells[5].replaceChildren(node('span', funding.text, 'block'));
        if (!funding.zero) {
          const note = node('span', funding.received ? 'received' : 'paid', 'margin-flow-note');
          if (funding.received) note.dataset.tone = 'gain';
          cells[5].append(note);
        }
      }
      const total = node('td', null, 'num margin-total');
      const net = flow(row.costs?.total_usd);
      total.append(node('span', net.text, 'margin-total-value'));
      if (row.costs) {
        if (net.received) {
          const note = node('span', 'received', 'margin-flow-note');
          note.dataset.tone = 'gain';
          total.append(note);
        }
        total.append(node('span', Fmt.bps(Math.abs(Number(row.costs.total_bps))), 'margin-total-bps'));
      }
      tr.append(venue, ...cells, total);
      return tr;
    }));
  }
  function render(data, body) {
    const pair = Boolean(body.pair);
    renderBest(data, pair);
    renderRows(data, pair);
    $('margin-legend').replaceChildren(...[
      describe(body),
      data.rank === 'cost' ? 'ranked by total cost' : 'ranked by capital needed',
      Number(data.rate) ? `capital at ${Number(data.rate)}% a year` : 'capital rate 0%',
    ].map(text => node('span', text)));
    setStatus('ready', data.completed_at ? Fmt.time(data.completed_at) : 'just now',
      `${data.venues.filter(row => row.status === 'ok').length}/${data.venues.length} ranked`, 'fresh', 'Captured ');
    pageState.ready();
  }
  function fail(message) {
    setStatus('error', 'Calculation failed', message, 'error');
    pageState.error('Margin comparison', () => calculate());
  }

  function save() {
    for (const [name, input] of Object.entries(inputs)) filters.set(name, input.value);
  }
  function restore() {
    for (const [name, input] of Object.entries(inputs)) {
      if (input.tagName === 'SELECT') filters.restoreSelect(input, name);
      else if (filters.get(name) !== null) input.value = filters.get(name);
    }
  }

  async function calculate() {
    syncControls();
    for (const input of Object.values(inputs)) input.removeAttribute('aria-invalid');
    let body;
    try {
      body = requestBody();
    } catch (error) {
      // A typing mistake is the reader's to fix, not a failed load: flag the
      // field and say what to enter, leaving the last results in place.
      error.input.setAttribute('aria-invalid', 'true');
      // Any calculation still running no longer matches the settings shown.
      generation++;
      setStatus('invalid', 'Check settings', error.message, 'warning');
      return;
    }
    save();
    if (controller) {
      queued = true;
      return;
    }
    const current = ++generation;
    controller = new AbortController();
    table.setAttribute('aria-busy', 'true');
    refresh.setAttribute('aria-disabled', 'true');
    setStatus('loading', 'Calculating…', describe(body));
    try {
      const response = await fetch(config.endpoint, {
        method: 'POST', headers: {'Content-Type': 'application/json', Accept: 'application/json'},
        body: JSON.stringify(body), signal: controller.signal, cache: 'no-store', credentials: 'same-origin',
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : `Calculation failed (${response.status}).`);
      if (current === generation && !queued) render(data, body);
    } catch (error) {
      if (current === generation && !queued) {
        fail(error instanceof SyntaxError ? 'The calculator returned an unreadable response.' : (error.message || 'Calculation failed.'));
      }
    } finally {
      controller = null;
      table.setAttribute('aria-busy', 'false');
      refresh.removeAttribute('aria-disabled');
      if (queued) {
        queued = false;
        calculate();
      }
    }
  }

  buildFeeControls();
  restore();
  syncControls();
  for (const input of Object.values(inputs)) input.addEventListener('change', () => calculate());
  // Dimmed (aria-disabled) while a calculation runs; it keeps focus, unlike disabled.
  refresh.addEventListener('click', () => {
    if (refresh.getAttribute('aria-disabled') !== 'true') calculate();
  });
  if (inputs.asset.options.length) calculate();
  else {
    refresh.disabled = true;
    pageState.empty('No crypto perpetual assets are configured. Add an asset to the symbol list to compare margin.');
  }
})();
