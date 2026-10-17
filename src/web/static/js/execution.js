(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const configNode = $('execution-config');
  if (!configNode) return;
  const config = JSON.parse(configNode.textContent);
  const table = $('execution-table');
  const asset = $('execution-asset');
  const side = $('execution-side');
  const refresh = $('execution-refresh');
  const status = $('execution-status');
  const refreshCadence = $('execution-refresh-cadence');
  const tbody = $('execution-rows');
  const app = $('execution-app');
  const tooltip = $('execution-price-tooltip');
  const schedules = config.fee_schedules || {};
  const notionals = config.notionals || [];
  const maxAge = Number(config.max_book_age_seconds) || 10;
  const refreshIntervalMs = 60 * 1000;
  const bestCards = notionals.map(notional => {
    const card = document.querySelector(`[data-best-notional="${notional}"]`);
    return {card, venue: card.querySelector('[data-best-venue]'),
      price: card.querySelector('[data-best-price]'), cost: card.querySelector('[data-best-cost]'),
      detail: card.querySelector('[data-best-detail]'), breakdown: card.querySelector('[data-best-breakdown]'),
      note: card.querySelector('[data-best-note]'), estimate: null};
  });
  const feeChoices = Object.create(null);
  const rows = new Map();
  let generation = 0;
  let controller = null;
  let snapshot = null;
  let receivedAt = 0;
  let completedAt = 0;
  let refreshTimer = null;
  let nextCaptureAt = 0;
  let pageActive = true;
  const priceData = new WeakMap();
  let tooltipTarget = null;
  let hoveredPrice = null;
  let tooltipHovered = false;
  let dismissedPrice = null;
  let touchFocus = false;
  let hideTooltipTimer = null;
  let renderedTooltipData = null;
  let renderedTooltipExpired = null;

  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  };
  const supplied = value => value !== null && value !== undefined && value !== '';
  // Number is used only for display shortening, never fee arithmetic or ranking.
  function amount(value) {
    if (!supplied(value)) return '—';
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return '—';
    const nonzero = /[1-9]/.test(String(value).split(/[eE]/, 1)[0]);
    if (numeric === 0) return nonzero ? `≈ ${String(value)}` : '0';
    const magnitude = Math.abs(numeric);
    if (magnitude > 0 && magnitude < 0.000001) return `≈ ${numeric.toExponential(4)}`;
    const places = magnitude >= 1000 ? 2 : magnitude >= 1 ? 4 : 8;
    const formatted = numeric.toLocaleString('en-US', {maximumFractionDigits: places});
    // Full supplied decimal strings remain available in the accessible detail.
    return `${Number(numeric.toFixed(places)) === numeric && String(value).replace(/[^0-9]/g, '').replace(/^0+/, '').length <= 15 ? '' : '≈ '}${formatted}`;
  }
  function exact(value, unit = '', missing = 'Unavailable') {
    return supplied(value) ? `${value}${unit ? ' ' + unit : ''}` : missing;
  }
  function formatted(value, unit = '', missing = 'Unavailable') {
    return supplied(value) ? `${amount(value)}${unit ? ' ' + unit : ''}` : missing;
  }
  function usd(value) {
    const formatted = amount(value);
    if (formatted === '—') return formatted;
    return formatted.startsWith('≈ ') ? `≈ $${formatted.slice(2)}` : `$${formatted}`;
  }
  function bps(value) {
    if (!supplied(value)) return 'Unknown (not zero)';
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return 'Unknown (not zero)';
    const nonzero = /[1-9]/.test(String(value).split(/[eE]/, 1)[0]);
    // Display only: each component and total comes directly from the server.
    if (Number(numeric.toFixed(2)) === 0) return nonzero ? '≈ 0 bps' : '0 bps';
    return `${numeric.toLocaleString('en-US', {maximumFractionDigits: 2})} bps`;
  }
  function allInCostDisplay(estimate) {
    const value = estimate.all_in_cost_bps;
    const numeric = Number(value);
    const known = (estimate.status === 'ok' || estimate.status === 'fx_missing') && supplied(value) && Number.isFinite(numeric);
    const text = known ? `${bps(value)} total` : 'Total cost unknown';
    const formula = 'taker fee bps + book slippage bps';
    const explanation = `${known ? `Exact total trading cost: ${value} bps` : text}. Total = ${formula}. Slippage measures the average fill against this venue's ${snapshot.side === 'buy' ? 'best ask for a buy' : 'best bid for a sell'}. For example, 6 bps fee + 4 bps slippage = 10 bps total. This is not a comparison with the common USD reference: spread and venue price differences are not added. Funding, settlement, gas, latency and account-specific extras are excluded.`;
    return {value, known, text, formula, explanation};
  }
  const statusLabels = {
    ok: 'Captured', stale: 'Quote expired', unavailable: 'Unavailable',
    invalid_book: 'Book unavailable', invalid_timestamp: 'Unverified quote age',
    fx_missing: 'USD conversion unavailable', fee_unknown: 'Fee unavailable',
    no_reference: 'No USD reference', insufficient_depth: 'Snapshot too shallow',
    order_limit: 'Above venue limit',
  };
  const label = value => statusLabels[value] || 'Unavailable';
  function sourceLink(url, text) {
    try {
      const parsed = new URL(url);
      if (parsed.protocol !== 'https:') return node('span', 'Official source unavailable');
      const link = node('a', text, 'execution-source-link');
      link.href = parsed.href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      return link;
    } catch (_) {
      return node('span', 'Official source unavailable');
    }
  }
  function detailLine(list, name, value) {
    list.append(node('dt', name), node('dd', value));
  }
  function tooltipLine(list, name, value, exactValue) {
    detailLine(list, name, value);
    if (value !== exactValue) list.lastElementChild.setAttribute('aria-label', `Exact: ${exactValue}`);
  }
  function signedPerCoin(value, currency, missing = 'Unknown (not zero)') {
    // The sign is the displayed buy/sell operation, not client-side fee math.
    return supplied(value) ? `${snapshot.side === 'buy' ? '+' : '−'} (${exact(value, currency)}) per ${snapshot.asset}` : missing;
  }
  function nativeAndUsd(native, dollars, venue, missing = 'Unavailable', format = exact) {
    const nativeText = format(native, `${venue.quote_currency || 'native quote'} per ${snapshot.asset}`, missing);
    return venue.quote_currency === 'USD' ? nativeText
      : `${nativeText} · ${format(dollars, `USD per ${snapshot.asset}`, 'USD unavailable (FX unknown)')}`;
  }
  function registerPriceTrigger(trigger, estimate, venue) {
    trigger.removeAttribute('title');
    if (!estimate.complete || !supplied(estimate.top_of_book_price)) {
      unregisterPriceTrigger(trigger);
      return;
    }
    const prior = priceData.get(trigger);
    if (prior?.estimate !== estimate || prior?.venue !== venue || prior?.snapshot !== snapshot) {
      priceData.set(trigger, {estimate, venue, snapshot});
    }
    trigger.dataset.priceTooltip = 'true';
  }
  function unregisterPriceTrigger(trigger) {
    if (tooltipTarget === trigger) hidePriceTooltip();
    priceData.delete(trigger);
    delete trigger.dataset.priceTooltip;
    trigger.removeAttribute('aria-describedby');
  }
  function hidePriceTooltip() {
    window.clearTimeout(hideTooltipTimer);
    hideTooltipTimer = null;
    tooltipTarget?.removeAttribute('aria-describedby');
    tooltipTarget = null;
    tooltipHovered = false;
    renderedTooltipData = null;
    renderedTooltipExpired = null;
    tooltip.hidden = true;
    tooltip.replaceChildren();
  }
  function clearPriceTooltip() {
    hidePriceTooltip();
    hoveredPrice = null;
    dismissedPrice = null;
  }
  function priceTrigger(target) {
    return target instanceof Element ? target.closest('[data-price-tooltip="true"]') : null;
  }
  function positionPriceTooltip() {
    if (!tooltipTarget || tooltip.hidden) return;
    const anchor = tooltipTarget.getBoundingClientRect();
    const viewport = window.visualViewport;
    const leftEdge = (viewport?.offsetLeft || 0) + 8;
    const topEdge = (viewport?.offsetTop || 0) + 8;
    const rightEdge = leftEdge + (viewport?.width || document.documentElement.clientWidth) - 16;
    const bottomEdge = topEdge + (viewport?.height || window.innerHeight) - 16;
    if (anchor.bottom < topEdge || anchor.top > bottomEdge || anchor.right < leftEdge || anchor.left > rightEdge) {
      hidePriceTooltip();
      return;
    }
    tooltip.style.maxWidth = `${Math.max(0, rightEdge - leftEdge)}px`;
    tooltip.style.maxHeight = `${Math.max(0, bottomEdge - topEdge)}px`;
    const box = tooltip.getBoundingClientRect();
    const left = Math.max(leftEdge, Math.min(anchor.right - box.width, rightEdge - box.width));
    const below = anchor.bottom + 6;
    const top = below + box.height <= bottomEdge ? below : anchor.top - box.height - 6;
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${Math.max(topEdge, Math.min(top, bottomEdge - box.height))}px`;
  }
  function updatePriceTooltip() {
    const data = tooltipTarget && priceData.get(tooltipTarget);
    if (!data || !tooltipTarget.isConnected || tooltipTarget.closest('[hidden]') || data.snapshot !== snapshot || !pageActive) {
      hidePriceTooltip();
      return;
    }
    const {estimate, venue} = data;
    const row = rows.get(venue.venue);
    const age = ageSeconds(venue);
    const expired = row?.stale || estimate.status === 'stale' || venue.status === 'stale' || (age !== null && age > maxAge);
    if (renderedTooltipData !== data || renderedTooltipExpired !== expired) {
      const heading = node('p', `${venue.label} · ${snapshot.side} · per 1 ${snapshot.asset}`, 'execution-tooltip-heading');
      heading.id = 'execution-price-tooltip-heading';
      const ageNode = node('p', null, 'execution-tooltip-age');
      ageNode.dataset.tooltipAge = 'true';
      ageNode.dataset.expired = String(Boolean(expired));
      const list = node('dl', null, 'execution-tooltip-list');
      tooltipLine(list, `OB best ${snapshot.side === 'buy' ? 'ask' : 'bid'}`,
        nativeAndUsd(estimate.top_of_book_price, estimate.top_of_book_price_usd, venue, 'Unavailable', formatted),
        nativeAndUsd(estimate.top_of_book_price, estimate.top_of_book_price_usd, venue));
      tooltipLine(list, 'Book slippage → gross fill',
        `${bps(estimate.price_impact_bps)} → ${nativeAndUsd(estimate.average_price, estimate.average_price_usd, venue, 'Unavailable', formatted)}`,
        `${exact(estimate.price_impact_bps, 'bps', 'Unknown (not zero)')} → ${nativeAndUsd(estimate.average_price, estimate.average_price_usd, venue)}`);
      tooltipLine(list, 'Taker fee rate', bps(venue.fee?.taker_bps), exact(venue.fee?.taker_bps, 'bps', 'Unknown (not zero)'));
      const missingEffectivePrice = !supplied(venue.fee?.taker_bps) ? 'Unknown fee — not ranked' : 'USD unavailable (FX unknown)';
      tooltipLine(list, 'Fee-adjusted price · USD',
        formatted(estimate.effective_price_usd, `USD per ${snapshot.asset}`, missingEffectivePrice),
        exact(estimate.effective_price_usd, `USD per ${snapshot.asset}`, missingEffectivePrice));
      tooltip.replaceChildren(heading, ageNode, list, node('p', '≈ Rounded · Select price for the full breakdown.', 'execution-tooltip-help'));
      renderedTooltipData = data;
      renderedTooltipExpired = expired;
    }
    tooltip.querySelector('[data-tooltip-age]').textContent = expired
      ? 'Expired — retained capture, not a current quote'
      : `Captured quote${age === null ? ' · age unknown' : ` · ${Math.floor(age)}s old`} · expires after ${maxAge}s`;
    positionPriceTooltip();
  }
  function showPriceTooltip(trigger) {
    if (!trigger || trigger === dismissedPrice || !priceData.has(trigger)) return;
    window.clearTimeout(hideTooltipTimer);
    if (tooltipTarget !== trigger) hidePriceTooltip();
    tooltipTarget = trigger;
    tooltip.hidden = false;
    trigger.setAttribute('aria-describedby', tooltip.id);
    updatePriceTooltip();
  }
  function scheduleTooltipHide() {
    window.clearTimeout(hideTooltipTimer);
    // A short one-shot bridge lets the pointer cross the gap onto the tooltip.
    hideTooltipTimer = window.setTimeout(() => {
      if (tooltipHovered || hoveredPrice === tooltipTarget || (!touchFocus && document.activeElement === tooltipTarget)) return;
      hidePriceTooltip();
    }, 180);
  }
  function bindPriceTooltip() {
    app.addEventListener('pointerover', event => {
      if (event.pointerType === 'touch') return;
      touchFocus = false;
      if (tooltip.contains(event.target)) {
        tooltipHovered = true;
        window.clearTimeout(hideTooltipTimer);
        return;
      }
      const trigger = priceTrigger(event.target);
      if (!trigger) return;
      hoveredPrice = trigger;
      showPriceTooltip(trigger);
    });
    app.addEventListener('pointerout', event => {
      if (event.pointerType === 'touch') return;
      if (tooltip.contains(event.target) && !tooltip.contains(event.relatedTarget)) {
        tooltipHovered = false;
        scheduleTooltipHide();
      }
      const trigger = priceTrigger(event.target);
      if (!trigger || trigger.contains(event.relatedTarget)) return;
      hoveredPrice = null;
      if (dismissedPrice === trigger && document.activeElement !== trigger) dismissedPrice = null;
      scheduleTooltipHide();
    });
    app.addEventListener('focusin', event => {
      if (!touchFocus) showPriceTooltip(priceTrigger(event.target));
    });
    app.addEventListener('focusout', event => {
      if (dismissedPrice === event.target && hoveredPrice !== event.target) dismissedPrice = null;
      scheduleTooltipHide();
    });
    document.addEventListener('pointerdown', event => {
      touchFocus = event.pointerType === 'touch';
      if (touchFocus) clearPriceTooltip();
    });
    document.addEventListener('keydown', event => {
      touchFocus = false;
      if (event.key === 'Escape' && tooltipTarget) {
        dismissedPrice = tooltipTarget;
        hidePriceTooltip();
      }
    });
    app.addEventListener('click', event => {
      const summary = event.target instanceof Element ? event.target.closest('[data-best-price]') : null;
      if (summary?.getAttribute('aria-disabled') === 'true') event.preventDefault();
    });
    window.addEventListener('resize', positionPriceTooltip);
    window.addEventListener('scroll', positionPriceTooltip, true);
    window.visualViewport?.addEventListener('resize', positionPriceTooltip);
    window.visualViewport?.addEventListener('scroll', positionPriceTooltip);
    new MutationObserver(() => {
      if (tooltipTarget && (!tooltipTarget.isConnected || tooltipTarget.closest('[hidden]'))) hidePriceTooltip();
    }).observe(app, {childList: true, subtree: true, attributes: true, attributeFilter: ['hidden']});
  }
  function selectedFeeText(venueId, responseFee) {
    const schedule = schedules[venueId];
    const tier = schedule?.tiers?.find(item => item.id === feeChoices[venueId]);
    const rate = tier ? tier.taker_bps : responseFee?.taker_bps;
    return supplied(rate) ? `${amount(rate)} bps fee` : 'Fee unknown';
  }
  function buildRow(venueId, fallbackLabel) {
    if (rows.has(venueId)) return rows.get(venueId);
    const schedule = schedules[venueId];
    const tr = node('tr');
    tr.dataset.venue = venueId;
    const venueCell = node('th', null, 'execution-venue-cell');
    venueCell.scope = 'row';
    const identity = node('span', null, 'execution-venue-dot');
    identity.setAttribute('aria-hidden', 'true');
    const name = node('span', schedule?.label || fallbackLabel || venueId, 'execution-venue-name');
    const heading = node('div', null, 'execution-venue-heading');
    heading.append(identity, name);
    const age = node('div', 'Pending', 'execution-age');
    age.setAttribute('aria-live', 'off');
    const state = node('div', 'Awaiting snapshot', 'execution-row-state');
    const bookDetails = node('details', null, 'execution-book-details');
    const bookSummary = node('summary', 'Details');
    bookSummary.setAttribute('aria-label', `${schedule?.label || fallbackLabel || venueId} quote and availability details`);
    bookDetails.append(bookSummary);
    const bookBody = node('div', null, 'execution-book-body');
    bookDetails.append(bookBody);
    bookDetails.hidden = true;
    const venueFee = node('span', selectedFeeText(venueId), 'execution-fee-rate');
    const meta = node('div', null, 'execution-venue-meta');
    const stats = node('div', null, 'execution-venue-stats');
    stats.append(venueFee, age);
    meta.append(stats, bookDetails);
    venueCell.append(heading, state, meta);
    const feeControl = node('label', null, 'execution-fee-control');
    feeControl.append(node('span', schedule?.label || fallbackLabel || venueId, 'text-data text-body'));
    const shell = node('span', null, 'select-shell');
    const select = node('select', null, 'select select-polished execution-fee-select');
    select.dataset.feeVenue = venueId;
    select.setAttribute('aria-label', `${schedule?.label || fallbackLabel || venueId} native VIP or account type`);
    for (const tier of schedule?.tiers || []) {
      const option = node('option', tier.label);
      option.value = tier.id;
      select.append(option);
    }
    if (schedule) {
      feeChoices[venueId] = schedule.default_tier;
      select.value = feeChoices[venueId];
      select.addEventListener('change', () => {
        feeChoices[venueId] = select.value;
        row.feeRate.textContent = selectedFeeText(venueId);
        row.venueFee.textContent = selectedFeeText(venueId);
        select.title = select.selectedOptions[0]?.textContent || '';
        capture('Fee assumption changed');
      });
    } else {
      select.append(node('option', 'Public fee unavailable'));
      select.disabled = true;
    }
    shell.append(select);
    const feeRate = node('div', selectedFeeText(venueId), 'execution-muted execution-fee-rate');
    feeControl.append(shell, feeRate);
    select.title = select.selectedOptions[0]?.textContent || '';
    $('execution-fee-controls').append(feeControl);
    const cells = notionals.map(notional => {
      const cell = node('td', 'Awaiting snapshot', 'execution-price-cell');
      cell.dataset.notional = notional;
      return cell;
    });
    tr.append(venueCell, ...cells);
    tbody.append(tr);
    const row = {tr, name, identity, age, state, bookDetails, bookBody, select, feeControl, feeRate, venueFee, cells, venue: null, prices: [], stale: false};
    rows.set(venueId, row);
    return row;
  }
  function populateFeeSources() {
    const container = $('execution-fee-sources');
    for (const [venueId, schedule] of Object.entries(schedules)) {
      const details = node('details', null, 'execution-fee-source');
      details.dataset.venue = venueId;
      details.append(node('summary', `${schedule.label} · checked ${schedule.checked_at || 'date unavailable'}`));
      const body = node('div', null, 'execution-fee-source-body');
      body.append(sourceLink(schedule.source_url, 'Official fee schedule'));
      const list = node('ul', null, 'execution-muted');
      for (const note of schedule.notes || []) list.append(node('li', note));
      body.append(list);
      const tiers = node('ul', null, 'execution-muted execution-fee-tiers');
      for (const tier of schedule.tiers || []) {
        tiers.append(node('li', `${tier.label}: ${supplied(tier.taker_bps) ? tier.taker_bps + ' bps taker' : 'unknown, not zero'}${tier.id === schedule.default_tier ? ' · default' : ''}`));
      }
      body.append(tiers);
      details.append(body);
      container.append(details);
    }
  }
  function ageSeconds(venue) {
    const timestamp = Date.parse(venue.captured_at);
    if (!Number.isFinite(timestamp) || !Number.isFinite(completedAt)) return null;
    return Math.max(0, (completedAt - timestamp + performance.now() - receivedAt) / 1000);
  }
  function updateRowAge(row) {
    const venue = row.venue;
    if (!venue) return;
    const age = ageSeconds(venue);
    const stale = venue.status === 'stale' || row.prices.some(price => price?.status === 'stale') || (age !== null && age > maxAge);
    row.stale = stale;
    row.tr.dataset.stale = String(stale);
    const basis = venue.timestamp_basis === 'venue' ? 'venue' : venue.timestamp_basis === 'fetch' ? 'receipt' : 'unknown';
    row.age.textContent = age === null ? 'Age unknown' : stale ? `Expired · ${Math.floor(age)}s` : `${Math.floor(age)}s · ${basis}`;
    row.age.title = `Age anchored to server completion, then advanced locally. Source: ${venue.captured_at || 'unknown'}. Received: ${venue.fetched_at || 'unknown'}. Timestamp basis: ${venue.timestamp_basis || 'unknown'}.`;
    row.state.textContent = label(venue.status);
    row.state.title = venue.error || label(venue.status);
    row.state.hidden = ['ok', 'stale', 'fee_unknown'].includes(venue.status) && !venue.error;
    row.cells.forEach((cell, index) => {
      const estimate = row.prices[index];
      const best = Boolean(estimate?.best && estimate.complete && estimate.status === 'ok' && venue.status === 'ok' && supplied(venue.fee?.taker_bps) && supplied(estimate.effective_price_usd) && age !== null && !stale);
      cell.classList.toggle('execution-best', best);
      cell.dataset.best = String(best);
      cell.dataset.stale = String(stale);
      cell.querySelectorAll('[data-price-freshness]').forEach(reading => {
        reading.textContent = stale ? 'Expired — retained capture, not a current quote' : 'Captured quote — refresh before trading';
      });
      const bestBadge = cell.querySelector('.execution-best-badge');
      if (bestBadge) bestBadge.hidden = !best;
    });
  }
  function renderBestSummary() {
    const buying = side.value === 'buy';
    $('execution-best-help').textContent = `${buying ? 'Lowest effective buy' : 'Highest effective sell'} price · after taker fees · captured quotes, not guaranteed fills`;
    $('execution-price-heading').textContent = `Effective ${buying ? 'buy' : 'sell'} price per 1 ${asset.value || 'coin'} · USD · after taker fees`;
    let custom = 0;
    for (const [venueId, row] of rows) {
      if (!row.tr.hidden && feeChoices[venueId] !== schedules[venueId]?.default_tier) custom++;
    }
    $('execution-fee-summary').textContent = custom ? `· ${custom} modified ${custom === 1 ? 'tier' : 'tiers'}` : '· base tiers';
    bestCards.forEach((view, index) => {
      const winners = [];
      let expired = false;
      for (const row of rows.values()) {
        if (row.tr.hidden) continue;
        if (row.cells[index].dataset.best === 'true') winners.push(row);
        if (row.prices[index]?.best && row.stale) expired = true;
      }
      if (winners.length) {
        const winner = winners[0];
        const estimate = winner.prices[index];
        const price = estimate.effective_price_usd;
        view.card.dataset.state = 'ready';
        view.venue.textContent = winners.map(row => row.name.textContent).join(' / ');
        view.price.textContent = usd(price);
        view.price.removeAttribute('aria-disabled');
        view.price.removeAttribute('tabindex');
        view.price.setAttribute('aria-label', `${winner.name.textContent}: ${price} USD per ${snapshot.asset}, after taker fees. Expand for price and fee breakdown.`);
        registerPriceTrigger(view.price, estimate, winner.venue);
        if (view.estimate !== estimate) {
          view.breakdown.replaceChildren(...priceBreakdown(estimate, winner.venue));
          view.estimate = estimate;
        }
        const cost = allInCostDisplay(estimate);
        view.cost.hidden = false;
        view.cost.textContent = cost.text;
        view.cost.title = cost.explanation;
        view.note.textContent = `USD per ${snapshot.asset} · after taker fees${winners.length > 1 ? ' · tied best' : ''}`;
        if (winners.length > 1) view.note.textContent += ` · breakdown: ${winner.name.textContent}`;
      } else {
        const pending = status.dataset.state === 'loading';
        const failed = status.dataset.state === 'error';
        view.card.dataset.state = expired ? 'expired' : pending ? 'loading' : 'unavailable';
        view.venue.textContent = pending ? 'Fetching prices…' : expired ? 'Quote expired' : failed ? 'Prices unavailable' : 'No full quote';
        view.price.textContent = '—';
        view.price.removeAttribute('title');
        unregisterPriceTrigger(view.price);
        view.price.removeAttribute('aria-label');
        view.price.setAttribute('aria-disabled', 'true');
        view.price.setAttribute('tabindex', '-1');
        view.detail.open = false;
        if (view.estimate) view.breakdown.replaceChildren();
        view.estimate = null;
        view.cost.hidden = true;
        view.cost.textContent = '';
        view.cost.removeAttribute('title');
        view.note.textContent = pending ? 'Comparing venues' : expired ? 'Minute refresh scheduled' : failed ? 'Minute refresh will retry' : 'See venue availability below';
      }
    });
  }
  function renderBookDetails(row, venue) {
    row.bookBody.replaceChildren();
    const data = node('dl', null, 'execution-detail-list');
    detailLine(data, 'Status at capture', label(venue.status));
    if (venue.error) detailLine(data, 'Provider diagnostic', venue.error);
    detailLine(data, 'Market', venue.symbol || 'Unknown');
    detailLine(data, 'Native price accounting', venue.quote_currency || 'Unknown');
    detailLine(data, 'Captured timestamp', venue.captured_at || 'Unknown');
    detailLine(data, 'REST received timestamp', venue.fetched_at || 'Unknown');
    detailLine(data, 'Age basis', venue.timestamp_basis === 'venue' ? 'Venue timestamp' : venue.timestamp_basis === 'fetch' ? 'Fetch receipt (venue time unavailable)' : 'Unknown');
    detailLine(data, 'Visible levels', `${venue.depth?.bid_levels ?? 'unknown'} bids / ${venue.depth?.ask_levels ?? 'unknown'} asks`);
    detailLine(data, 'Public maximum base size', exact(venue.limits?.max_quantity, snapshot.asset, 'Not published / unknown'));
    detailLine(data, 'Public maximum native notional', exact(venue.limits?.max_quote_notional, venue.quote_currency, 'Not published / unknown'));
    row.bookBody.append(data);
    for (const note of venue.notes || []) row.bookBody.append(node('p', note));
    row.bookDetails.hidden = false;
  }
  function priceBreakdown(estimate, venue) {
    const feeUnknown = !supplied(venue.fee?.taker_bps) || estimate.status === 'fee_unknown';
    const fxUnknown = venue.status === 'fx_missing' || estimate.status === 'fx_missing';
    const {value: costValue, known: costKnown, formula} = allInCostDisplay(estimate);
    const nativeUnit = `${venue.quote_currency || 'native quote'} per ${snapshot.asset}`;
    const usdUnit = `USD per ${snapshot.asset}`;
    const list = node('dl', null, 'execution-detail-list');
    detailLine(list, 'Gross fill per coin · native', exact(estimate.average_price, nativeUnit));
    detailLine(list, 'Gross fill per coin · USD', exact(estimate.average_price_usd, usdUnit, fxUnknown ? 'FX unknown — not ranked' : 'Unavailable'));
    detailLine(list, 'Fee-adjusted fill per coin · USD', exact(estimate.effective_price_usd, usdUnit, feeUnknown ? 'Unknown fee — not ranked' : fxUnknown ? 'FX unknown — not ranked' : 'Unavailable'));
    detailLine(list, `OB best ${snapshot.side === 'buy' ? 'ask' : 'bid'} · native`, exact(estimate.top_of_book_price, nativeUnit));
    detailLine(list, `OB best ${snapshot.side === 'buy' ? 'ask' : 'bid'} · USD`, exact(estimate.top_of_book_price_usd, usdUnit, fxUnknown ? 'FX unknown' : 'Unavailable'));
    detailLine(list, `Taker fee per coin · native · ${snapshot.side === 'buy' ? 'added' : 'deducted'}`, signedPerCoin(estimate.fee_per_coin, venue.quote_currency || 'native quote', feeUnknown ? 'Unknown (not zero)' : 'Unavailable'));
    detailLine(list, 'Taker fee per coin · USD', signedPerCoin(estimate.fee_per_coin_usd, 'USD', feeUnknown ? 'Unknown (not zero)' : fxUnknown ? 'FX unknown' : 'Unavailable'));
    const freshness = node('p', rows.get(venue.venue)?.stale ? 'Expired — retained capture, not a current quote' : 'Captured quote — refresh before trading', 'execution-price-state');
    freshness.dataset.priceFreshness = 'true';
    const components = node('dl', null, 'execution-detail-list execution-cost-components');
    const feeComponent = feeUnknown ? 'Unknown fee' : `${bps(venue.fee?.taker_bps)} fee`;
    const slippageComponent = `${bps(estimate.price_impact_bps)} slippage`;
    const equation = `${feeComponent} + ${slippageComponent} = ${costKnown && !feeUnknown ? bps(costValue) + ' total' : 'unknown total (not zero)'}`;
    detailLine(components, 'One-way cost · fee + book slippage', equation);
    detailLine(components, 'Exact taker fee rate', exact(venue.fee?.taker_bps, 'bps', 'Unknown (not zero)'));
    detailLine(components, `Exact book slippage vs venue's ${snapshot.side === 'buy' ? 'best ask' : 'best bid'}`, exact(estimate.price_impact_bps, 'bps'));
    detailLine(components, 'Exact total cost', costKnown && !feeUnknown ? exact(costValue, 'bps') : 'Unknown (not zero)');
    detailLine(components, 'Whole-order one-way taker fee · USD', exact(estimate.fee_usd, 'USD', feeUnknown ? 'Unknown (not zero)' : 'Unavailable'));
    const diagnostics = node('dl', null, 'execution-detail-list');
    detailLine(diagnostics, 'Common unrounded base quantity', exact(estimate.quantity, snapshot.asset));
    detailLine(diagnostics, 'Visible filled quantity', exact(estimate.filled_quantity, snapshot.asset));
    detailLine(diagnostics, 'Native fee assumption', `${venue.fee?.tier_label || 'Unknown'} · ${exact(venue.fee?.taker_bps, 'bps', 'Unknown (not zero)')}`);
    const methodology = node('details', null, 'execution-disclosure execution-price-method');
    methodology.append(node('summary', 'Cost basis & assumptions'));
    const methodBody = node('div', null, 'execution-disclosure-body');
    methodBody.append(node('p', `${formula}. Slippage is measured against this venue's ${snapshot.side === 'buy' ? 'best ask' : 'best bid'}, not the common USD reference. Spread, venue price differences, funding, settlement, gas, latency and account-specific extras are excluded. FX is not needed for native book bps; unknown FX prices remain unranked. Best compares fee-adjusted USD prices, not cost bps. Per-coin fees are separate from whole-order fee totals.`, 'text-label text-dim'));
    methodology.append(methodBody);
    return [list, freshness, components, diagnostics, methodology];
  }
  function renderPrice(cell, estimate, venue) {
    cell.replaceChildren();
    cell.removeAttribute('title');
    cell.classList.remove('execution-best');
    cell.dataset.best = 'false';
    cell.dataset.status = estimate?.status || 'unavailable';
    if (!estimate) {
      cell.append(node('div', 'Unavailable', 'execution-price-state'));
      return;
    }
    const badges = node('div', null, 'execution-price-badges');
    const bestBadge = node('span', 'Best', 'execution-best-badge');
    bestBadge.hidden = true;
    badges.append(bestBadge);
    const rejected = ['insufficient_depth', 'order_limit', 'no_reference', 'invalid_book', 'invalid_timestamp', 'unavailable'].includes(estimate.status);
    const feeUnknown = !supplied(venue.fee?.taker_bps) || estimate.status === 'fee_unknown';
    const fxUnknown = venue.status === 'fx_missing' || estimate.status === 'fx_missing';
    let price = null;
    let unit = 'USD';
    let priceLabel = 'After taker fee';
    if (!rejected && estimate.complete) {
      if (fxUnknown) {
        price = estimate.average_price;
        unit = venue.quote_currency || 'native quote';
        priceLabel = 'Gross · FX unknown';
      } else if (feeUnknown) {
        price = estimate.average_price_usd;
        priceLabel = 'Before fees';
      } else {
        price = estimate.effective_price_usd;
      }
    }
    if (supplied(price)) {
      const details = node('details', null, 'execution-price-detail');
      const summary = node('summary');
      summary.append(node('span', unit === 'USD' ? usd(price) : `${amount(price)} ${unit}`, 'execution-price-value'));
      const {text: costText, explanation: costExplanation} = allInCostDisplay(estimate);
      const costLine = node('span', costText, 'execution-all-in-cost');
      summary.append(costLine);
      if (feeUnknown || fxUnknown) summary.append(node('span', priceLabel, 'execution-price-label'));
      summary.setAttribute('aria-label', `${priceLabel}: ${price} ${unit} per ${snapshot.asset}. ${costExplanation} Expand for price and fee breakdown.`);
      registerPriceTrigger(summary, estimate, venue);
      details.append(summary, ...priceBreakdown(estimate, venue));
      cell.append(details);
      if (fxUnknown && feeUnknown) cell.append(node('div', 'Fee unknown · before fees', 'execution-price-state'));
    } else {
      cell.append(node('div', label(estimate.status === 'ok' ? 'unavailable' : estimate.status), 'execution-price-state'));
      if (estimate.status === 'insufficient_depth') {
        const fill = node('div', `${amount(estimate.fill_percent)}% captured`, 'execution-fill');
        const depth = snapshot.side === 'buy' ? venue.depth?.ask_levels : venue.depth?.bid_levels;
        const explanation = `${supplied(depth) ? depth + ' captured price levels cover ' : 'Captured depth covers '}${exact(estimate.filled_quantity, snapshot.asset)} of ${exact(estimate.quantity, snapshot.asset)} requested. This is a snapshot coverage limit, not a measure of the venue's full liquidity. No full-size price is extrapolated.`;
        fill.title = explanation;
        cell.title = explanation;
        cell.append(fill);
      }
      if (estimate.status === 'order_limit') cell.title = 'Exceeds the published market-order maximum; expand Details for the cap.';
    }
    cell.append(badges);
  }
  function renderReference() {
    $('execution-reference').textContent = supplied(snapshot.reference_price_usd)
      ? `${snapshot.asset} reference: ${amount(snapshot.reference_price_usd)} USD`
      : `${snapshot.asset}: no fresh common USD reference; full-size comparisons unavailable.`;
    $('execution-reference').title = `Exact reference USD midpoint median: ${exact(snapshot.reference_price_usd, 'USD')}. Reference-USD entry comparison, not a cash purchase or conversion commitment.`;
    for (const header of document.querySelectorAll('[data-execution-notional]')) {
      const index = notionals.indexOf(header.dataset.executionNotional);
      const quantity = snapshot.quantities?.[index];
      const target = header.querySelector('.execution-quantity');
      target.textContent = supplied(quantity) ? `${amount(quantity)} ${snapshot.asset}` : 'Base qty unavailable';
      target.title = supplied(quantity) ? `Common unrounded base quantity: ${quantity} ${snapshot.asset}; reference notional ${header.dataset.executionNotional} USD, not an all-in cash budget.` : 'No fresh common USD reference price.';
    }
    const fx = snapshot.fx || {};
    const rates = Object.entries(fx.rates || {}).filter(([currency]) => currency !== 'USD')
      .map(([currency, rate]) => `${currency} ${amount(rate)}`).join(' · ');
    const fxNode = $('execution-fx');
    const fxTime = fx.ts && Number.isFinite(Date.parse(fx.ts))
      ? new Date(fx.ts).toLocaleTimeString('en-GB', {hour12: false, timeZone: 'UTC'}) + ' UTC' : null;
    fxNode.title = `Exact USD reference rates: ${Object.entries(fx.rates || {}).map(([currency, rate]) => `${currency} = ${rate} USD`).join('; ')}. Checked: ${fx.ts || 'unavailable'}. ${fx.error || 'No stablecoin peg assumed; not an executed conversion.'}`;
    fxNode.replaceChildren(node('span', `USD FX · ${rates || 'stablecoin rates unavailable'}${fxTime ? ' · ' + fxTime : ''}${fx.error ? ' · incomplete' : ''} · `));
    const source = sourceLink(fx.source_url, 'Source');
    source.setAttribute('aria-label', fx.source || 'Official reference FX source');
    fxNode.append(source);
  }
  function renderSnapshot(data) {
    clearPriceTooltip();
    snapshot = data;
    completedAt = Date.parse(data.completed_at);
    receivedAt = performance.now();
    renderReference();
    const visible = new Set();
    for (const venue of data.venues) {
      visible.add(venue.venue);
      const row = buildRow(venue.venue, venue.label);
      row.tr.hidden = false;
      row.venue = venue;
      row.prices = notionals.map(notional => venue.estimates?.find(estimate => estimate.notional_usd === notional));
      row.name.textContent = venue.label;
      row.identity.style.backgroundColor = colorFor(venue.identity);
      row.feeRate.textContent = selectedFeeText(venue.venue, venue.fee);
      row.venueFee.textContent = selectedFeeText(venue.venue, venue.fee);
      renderBookDetails(row, venue);
      row.cells.forEach((cell, index) => renderPrice(cell, row.prices[index], venue));
      updateRowAge(row);
    }
    for (const [venueId, row] of rows) {
      row.tr.hidden = !visible.has(venueId);
      row.feeControl.hidden = row.tr.hidden;
    }
    if (!data.venues.length) {
      const empty = node('tr', null, 'execution-empty-row');
      const cell = node('td', 'No configured native perpetual venues for this coin.', 'execution-empty');
      cell.colSpan = notionals.length + 1;
      empty.append(cell);
      tbody.append(empty);
    }
    const count = data.venues.filter(venue => supplied(venue.fetched_at)).length;
    status.textContent = `${data.asset} ${data.side} · ${count}/${data.venues.length} books captured. ${data.venues.length ? 'Minute refresh scheduled; refresh now for current prices.' : 'Choose another coin.'}`;
    status.dataset.state = 'ready';
    renderBestSummary();
  }
  function clearQuotes(reason) {
    clearPriceTooltip();
    snapshot = null;
    for (const empty of tbody.querySelectorAll('.execution-empty-row')) empty.remove();
    $('execution-reference').textContent = `${asset.value || 'Coin'} reference: requesting new snapshots; previous quotes cleared.`;
    $('execution-reference').removeAttribute('title');
    $('execution-fx').textContent = 'USD reference FX: awaiting new snapshot; no stablecoin peg assumed.';
    $('execution-fx').removeAttribute('title');
    document.querySelectorAll('.execution-quantity').forEach(target => {
      target.textContent = 'Base qty pending';
      target.removeAttribute('title');
    });
    for (const row of rows.values()) {
      row.venue = null;
      row.prices = [];
      row.tr.hidden = false;
      row.tr.dataset.stale = 'false';
      row.age.textContent = 'Pending';
      row.age.removeAttribute('title');
      row.state.hidden = false;
      row.state.removeAttribute('title');
      row.venueFee.textContent = selectedFeeText(row.tr.dataset.venue);
      row.state.textContent = 'Fetching quote';
      row.bookDetails.hidden = true;
      for (const cell of row.cells) {
        cell.replaceChildren(node('span', 'Fetching…', 'execution-muted'));
        cell.removeAttribute('title');
        cell.classList.remove('execution-best');
        cell.dataset.best = 'false';
        cell.dataset.stale = 'false';
        cell.dataset.status = 'loading';
      }
    }
    status.textContent = `${reason}. Fetching ${asset.value} ${side.value} prices…`;
    status.dataset.state = 'loading';
    renderBestSummary();
  }
  function pageVisible() {
    return pageActive && !document.hidden;
  }
  function updateRefreshCadence() {
    if (!asset.options.length) {
      refreshCadence.textContent = 'Automatic refresh unavailable: no configured coins.';
    } else if (!pageVisible()) {
      refreshCadence.textContent = 'Auto-refresh every minute · paused while this page is hidden';
    } else {
      const remaining = Math.max(0, Math.ceil((nextCaptureAt - Date.now()) / 1000));
      refreshCadence.textContent = `Auto-refresh every minute · ${controller ? 'capture in progress · ' : ''}next in ${remaining}s`;
    }
  }
  function stopRefreshTimer() {
    window.clearTimeout(refreshTimer);
    refreshTimer = null;
  }
  function scheduleRefreshTimer() {
    stopRefreshTimer();
    updateRefreshCadence();
    if (!pageVisible() || !asset.options.length) return;
    refreshTimer = window.setTimeout(refreshClock, Math.max(1, Math.min(1000, nextCaptureAt - Date.now())));
  }
  function skipElapsedRefreshes(now) {
    if (nextCaptureAt <= now) {
      // Keep the cadence anchored to capture start, rather than retrying at completion.
      nextCaptureAt += (Math.floor((now - nextCaptureAt) / refreshIntervalMs) + 1) * refreshIntervalMs;
    }
  }
  function updateSnapshotAge() {
    if (!snapshot) return;
    let captured = 0;
    let allStale = true;
    for (const row of rows.values()) {
      if (row.tr.hidden) continue;
      updateRowAge(row);
      if (row.venue?.captured_at) {
        captured++;
        allStale &&= row.stale;
      }
    }
    renderBestSummary();
    if (tooltipTarget) updatePriceTooltip();
    if (captured && allStale && status.dataset.state === 'ready') {
      status.dataset.state = 'stale';
      status.textContent = `${snapshot.asset} quotes expired after ${maxAge}s. The table retains old readings until the next minute refresh; refresh now for a new comparison.`;
    }
  }
  function refreshClock() {
    stopRefreshTimer();
    if (!pageVisible() || !asset.options.length) {
      updateRefreshCadence();
      return;
    }
    updateSnapshotAge();
    const now = Date.now();
    if (now >= nextCaptureAt) {
      if (!controller) {
        capture('Automatic refresh');
        return;
      }
      skipElapsedRefreshes(now);
    }
    scheduleRefreshTimer();
  }
  async function capture(reason) {
    clearPriceTooltip();
    if (!pageVisible() || !asset.options.length) return;
    const current = ++generation;
    controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    nextCaptureAt = Date.now() + refreshIntervalMs;
    clearQuotes(reason);
    table.setAttribute('aria-busy', 'true');
    refresh.textContent = 'Refreshing…';
    const request = {asset: asset.value, side: side.value, fees: {...feeChoices}};
    scheduleRefreshTimer();
    try {
      const response = await fetch(config.endpoint, {
        method: 'POST', headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
        body: JSON.stringify(request), signal, cache: 'no-store', credentials: 'same-origin',
      });
      const data = await response.json();
      if (current !== generation || signal.aborted) return;
      if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : `Snapshot request failed (${response.status}).`);
      if (data.asset !== request.asset || data.side !== request.side || !Array.isArray(data.venues) || !Number.isFinite(Date.parse(data.completed_at))) {
        throw new Error('Snapshot response did not match the requested comparison.');
      }
      renderSnapshot(data);
    } catch (error) {
      if (current !== generation || signal.aborted) return;
      clearQuotes('Snapshot request failed');
      status.dataset.state = 'error';
      status.textContent = `No current estimate. ${error instanceof SyntaxError ? 'Price service returned an unreadable response.' : error.message || 'Price request failed.'} Minute refresh will try again while this page is visible; refresh now to retry sooner.`;
      for (const row of rows.values()) {
        row.state.textContent = 'Snapshot request failed';
        row.age.textContent = 'No snapshot';
        for (const cell of row.cells) {
          cell.textContent = 'No current estimate';
          cell.classList.remove('execution-best');
          cell.dataset.best = 'false';
          cell.dataset.status = 'unavailable';
        }
      }
      renderBestSummary();
    } finally {
      if (current === generation) {
        table.setAttribute('aria-busy', 'false');
        refresh.textContent = 'Refresh prices';
        skipElapsedRefreshes(Date.now());
        controller = null;
        scheduleRefreshTimer();
      }
    }
  }

  bindPriceTooltip();
  tbody.replaceChildren();
  for (const [venueId, schedule] of Object.entries(schedules)) buildRow(venueId, schedule.label);
  populateFeeSources();
  asset.addEventListener('change', () => capture('Coin changed'));
  side.addEventListener('change', () => capture('Direction changed'));
  refresh.addEventListener('click', () => capture('Manual refresh'));
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearPriceTooltip();
    refreshClock();
  });
  window.addEventListener('pageshow', () => {
    pageActive = true;
    refreshClock();
  });
  window.addEventListener('pagehide', () => {
    clearPriceTooltip();
    pageActive = false;
    stopRefreshTimer();
    updateRefreshCadence();
    ++generation;
    if (!controller) return;
    controller.abort();
    controller = null;
    table.setAttribute('aria-busy', 'false');
    refresh.textContent = 'Refresh prices';
    status.dataset.state = 'error';
    status.textContent = 'Comparison interrupted. Minute refresh resumes when this page is visible; refresh now to retry sooner.';
    for (const row of rows.values()) {
      row.state.textContent = 'Capture interrupted';
      row.age.textContent = 'No snapshot';
      for (const cell of row.cells) {
        cell.textContent = 'No current estimate';
        cell.dataset.status = 'unavailable';
      }
    }
    renderBestSummary();
  });
  if (asset.options.length) {
    nextCaptureAt = Date.now();
    if (pageVisible()) capture('Initial capture');
    else scheduleRefreshTimer();
  }
  else {
    refresh.disabled = true;
    side.disabled = true;
    asset.disabled = true;
    status.textContent = 'No configured crypto perpetual assets are available.';
    status.dataset.state = 'empty';
    renderBestSummary();
    updateRefreshCadence();
  }
})();
