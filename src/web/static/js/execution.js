(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const configNode = $('execution-config');
  if (!configNode) return;
  const config = JSON.parse(configNode.textContent);
  const table = $('execution-table');
  const asset = $('execution-asset');
  const trade = $('execution-trade');
  const hold = $('execution-hold');
  const refresh = $('execution-refresh');
  const status = $('execution-status');
  const statusState = $('execution-status-state');
  const statusContext = $('execution-status-context');
  const refreshCadence = $('execution-refresh-cadence');
  const warning = $('execution-warning');
  const pageState = Overseer.pageState();
  const tbody = $('execution-rows');
  const app = $('execution-app');
  const tooltip = $('execution-price-tooltip');
  const schedules = config.fee_schedules || {};
  const holdLabels = Object.fromEntries((config.holds || []).map(([hours, text]) => [String(hours), text]));
  // Trade-size labels come from the template's notional_label macro, the one source.
  const sizeLabels = Object.fromEntries([...document.querySelectorAll('[data-execution-notional]')]
    .map(header => [header.dataset.executionNotional, header.querySelector('.execution-notional').textContent.trim()]));
  const sizeLabel = notional => sizeLabels[notional] || Fmt.usd(notional, 0);
  // A cost that exists but is not known is never shown as zero.
  const UNKNOWN = 'Unknown, not zero';
  const notionals = config.notionals || [];
  const maxAge = Number(config.max_book_age_seconds) || 10;
  const refreshIntervalMs = 60 * 1000;
  const bestCards = notionals.map(notional => {
    const card = document.querySelector(`[data-best-notional="${notional}"]`);
    return {card, venue: card.querySelector('[data-best-venue]'), dot: card.querySelector('[data-best-dot]'),
      price: card.querySelector('[data-best-price]'), cost: card.querySelector('[data-best-cost]'),
      detail: card.querySelector('[data-best-detail]'), breakdown: card.querySelector('[data-best-breakdown]'),
      note: card.querySelector('[data-best-note]'), badge: card.querySelector('[data-best-badge]'),
      estimate: null, model: null, rendered: null};
  });
  const feeChoices = Object.create(null);
  const rows = new Map();
  let generation = 0;
  let controller = null;
  let snapshot = null;
  // Display precision for the current comparison, from the asset's price magnitude.
  let snapshotDigits = 2;
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
  const skeleton = width => {
    const element = node('span', null, 'skeleton');
    element.style.width = width;
    element.setAttribute('aria-hidden', 'true');
    return element;
  };
  const setText = (element, text) => {
    if (element.textContent !== text || element.children.length) element.textContent = text;
  };
  const supplied = value => value !== null && value !== undefined && value !== '';
  const isRoundTrip = value => value === 'buy_sell' || value === 'sell_buy';
  const holding = snap => Number(snap?.hold_hours) > 0;
  // Plain words for the comparison: 'buy', 'sell, then hold 1 week',
  // 'buy, hold 1 day, then sell'.
  function tradePhrase(value, hours) {
    const held = Number(hours) ? `hold ${holdLabels[String(hours)] || `${hours} hours`}` : '';
    if (!isRoundTrip(value)) return held ? `${value}, then ${held}` : value;
    const [open, close] = value === 'buy_sell' ? ['buy', 'sell'] : ['sell', 'buy'];
    return held ? `${open}, ${held}, then ${close}` : `${open}, then ${close}`;
  }
  // Number of decimal places that shows about `figures` significant figures.
  function sigDigits(value, figures) {
    const magnitude = Math.abs(Number(value));
    if (!magnitude) return 0;
    return Math.max(0, figures - 1 - Math.floor(Math.log10(magnitude)));
  }
  // A server decimal string written out in full, without rounding: exponent
  // notation expanded, trailing fractional zeros dropped, integer digits grouped.
  function exactNumber(value) {
    const text = String(value).trim();
    const match = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(text);
    if (!match || !(match[2] || match[3])) return text;
    const [, sign, whole = '', fraction = '', exponent = '0'] = match;
    let digits = whole + fraction;
    let point = whole.length + Number(exponent);
    if (point < 0) {
      digits = '0'.repeat(-point) + digits;
      point = 0;
    }
    if (point > digits.length) digits += '0'.repeat(point - digits.length);
    const integer = (digits.slice(0, point).replace(/^0+/, '') || '0').replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    const decimals = digits.slice(point).replace(/0+$/, '');
    const zero = integer === '0' && !decimals;
    return `${sign === '-' && !zero ? '−' : ''}${integer}${decimals ? `.${decimals}` : ''}`;
  }
  function exact(value, unit = '', missing = 'Unavailable') {
    return supplied(value) ? `${exactNumber(value)}${unit ? ' ' + unit : ''}` : missing;
  }
  // Costs in bps, two places; a tiny non-zero cost reads "<0.01 bps", never zero.
  function bps(value) {
    return supplied(value) && Number.isFinite(Number(value)) ? Fmt.bps(value, 2) : UNKNOWN;
  }
  function signedBps(value) {
    return supplied(value) && Number.isFinite(Number(value)) ? Fmt.bps(value, 2, {sign: true}) : UNKNOWN;
  }
  // Fee rates in bps, one place, at their published precision (3.0, 3.75).
  function feeRate(value) {
    return supplied(value) && Number.isFinite(Number(value)) ? Fmt.feeBps(value) : UNKNOWN;
  }
  function fundingRateText(funding) {
    const numeric = Number(funding?.rate) * 100;
    if (!supplied(funding?.rate) || !Number.isFinite(numeric)) return funding?.unavailable || 'Funding unknown';
    const hours = Number(funding.interval_hours);
    return `${Fmt.signedPercent(numeric, 4)} per ${Fmt.number(hours, Number.isInteger(hours) ? 0 : 1)}h`;
  }
  function fundingBasisText(funding) {
    if (!funding) return 'No hold';
    const source = funding.basis === 'current'
      ? 'current rate, pro rata over the hold'
      : 'previous 24 hours of settled funding, repeated over the hold';
    return `${funding.unavailable ? `${funding.unavailable} — not ranked` : fundingRateText(funding)} · ${source}`;
  }
  // One precision per comparison: about five significant figures at the asset's
  // price magnitude (2 dp for BTC or ETH, more for low-priced assets).
  function priceDigits(snap) {
    let magnitude = Math.abs(Number(snap?.reference_price_usd));
    if (!Number.isFinite(magnitude) || magnitude === 0) {
      const sample = (snap?.venues || []).flatMap(venue => venue.estimates || [])
        .map(estimate => Math.abs(Number(estimate.average_price_usd ?? estimate.average_price ?? estimate.buy?.average_price_usd)))
        .find(value => Number.isFinite(value) && value > 0);
      magnitude = sample || 0;
    }
    if (!magnitude) return 2;
    return Math.min(10, Math.max(2, 4 - Math.floor(Math.log10(magnitude))));
  }
  const usdPrice = (value, digits) => Fmt.usd(value, digits);
  // A base quantity to four significant figures, whole units from 1,000 up.
  function quantityText(value) {
    if (!supplied(value) || !Number.isFinite(Number(value))) return '—';
    return Fmt.number(value, Math.abs(Number(value)) >= 1000 ? 0 : sigDigits(value, 4));
  }
  // The cost line beneath each value. A total that cannot be known shows the
  // part that can, labelled, rather than nothing or a fake zero.
  function allInCostDisplay(estimate, snap = snapshot) {
    const roundTrip = isRoundTrip(snap.side);
    const held = holding(snap);
    const value = roundTrip || held ? estimate.total_cost_bps : estimate.all_in_cost_bps;
    const known = supplied(value) && Number.isFinite(Number(value))
      && (estimate.status === 'ok' || (!roundTrip && estimate.status === 'fx_missing'));
    const partial = roundTrip
      ? (supplied(estimate.trading_cost_bps) ? [estimate.trading_cost_bps, 'before funding']
        : supplied(estimate.spread_slippage_bps) ? [estimate.spread_slippage_bps, held ? 'before fees and funding' : 'before fees'] : null)
      : held && supplied(estimate.all_in_cost_bps) ? [estimate.all_in_cost_bps, 'before funding'] : null;
    const text = known ? `${bps(value)} total` : partial ? `${bps(partial[0])} ${partial[1]}` : 'Total cost unknown';
    const formula = roundTrip
      ? `spread, book slippage and both taker fees${held ? ', plus funding while held' : ''}`
      : `taker fee + book slippage${held ? ' + funding' : ''}`;
    const against = roundTrip ? '' : `, with slippage against this venue's ${snap.side === 'buy' ? 'best ask' : 'best bid'}`;
    const explanation = known
      ? `Exact ${roundTrip ? 'round-trip cost' : 'total trading cost'}: ${exact(value, 'bps')}${roundTrip ? ' of the trade size' : ''}, ${formula}${against}.`
      : `${text}, ${formula}${against}.`;
    return {value, known, text, formula, explanation};
  }
  const statusLabels = {
    ok: 'Captured', stale: 'Quote expired', unavailable: 'Unavailable',
    invalid_book: 'Book unavailable', invalid_timestamp: 'Unverified quote age',
    fx_missing: 'USD conversion unavailable', fee_unknown: 'Fee unknown',
    no_reference: 'No USD reference', insufficient_depth: 'Snapshot too shallow',
    order_limit: 'Above venue limit', funding_unknown: 'Funding unknown',
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
  function signedPerCoin(value, currency, missing = UNKNOWN) {
    // The sign is the displayed buy/sell operation, not client-side fee math.
    return supplied(value) ? `${snapshot.side === 'buy' ? '+' : '−'} (${exact(value, currency)}) per ${snapshot.asset}` : missing;
  }
  function nativeAndUsd(native, dollars, venue, missing = 'Unavailable', format = exact, snap = snapshot) {
    const nativeText = format(native, `${venue.quote_currency || 'native quote'} per ${snap.asset}`, missing);
    return venue.quote_currency === 'USD' ? nativeText
      : `${nativeText} · ${format(dollars, `USD per ${snap.asset}`, 'USD unavailable (FX unknown)')}`;
  }
  function registerPriceTrigger(trigger, estimate, venue, snap = snapshot) {
    trigger.removeAttribute('title');
    const priced = isRoundTrip(snap.side) ? supplied(estimate.spread_slippage) : supplied(estimate.top_of_book_price);
    if (!estimate.complete || !priced) {
      unregisterPriceTrigger(trigger);
      return;
    }
    const prior = priceData.get(trigger);
    if (prior?.estimate !== estimate || prior?.venue !== venue || prior?.snapshot !== snap) {
      priceData.set(trigger, {estimate, venue, snapshot: snap});
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
  function roundTripTooltipLines(list, estimate, venue, snap, rounded) {
    const unit = `USD per ${snap.asset}`;
    for (const [name, leg] of [['Buy', estimate.buy], ['Sell', estimate.sell]]) {
      const afterFee = supplied(leg?.effective_price_usd);
      const value = afterFee ? leg.effective_price_usd : leg?.average_price_usd;
      tooltipLine(list, `${name} fill · ${afterFee ? 'after fee' : 'before fees'}`,
        rounded(value, unit, 'USD unavailable (FX unknown)'), exact(value, unit, 'USD unavailable (FX unknown)'));
    }
    tooltipLine(list, 'Spread + slippage',
      supplied(estimate.spread_slippage_usd) ? `${Fmt.usd(estimate.spread_slippage_usd)} · ${bps(estimate.spread_slippage_bps)}` : 'USD unavailable (FX unknown)',
      exact(estimate.spread_slippage_usd, 'USD', 'USD unavailable (FX unknown)'));
    tooltipLine(list, 'Taker fees, both legs', supplied(estimate.fees_usd) ? Fmt.usd(estimate.fees_usd) : UNKNOWN,
      exact(estimate.fees_usd, 'USD', UNKNOWN));
    if (holding(snap)) {
      const missing = venue.funding?.unavailable ? `${venue.funding.unavailable} — not ranked` : 'Unavailable';
      tooltipLine(list, `Funding · ${holdLabels[String(snap.hold_hours)]}`,
        supplied(estimate.funding_usd) ? `${Fmt.usd(estimate.funding_usd)} · ${signedBps(estimate.funding_bps)}` : missing,
        exact(estimate.funding_usd, 'USD', missing));
    }
    tooltipLine(list, 'Total', supplied(estimate.total_cost_usd) ? `${Fmt.usd(estimate.total_cost_usd)} · ${bps(estimate.total_cost_bps)}` : UNKNOWN,
      exact(estimate.total_cost_usd, 'USD', UNKNOWN));
  }
  function updatePriceTooltip() {
    const data = tooltipTarget && priceData.get(tooltipTarget);
    // Table cells are rebuilt per capture; best cards re-register (or drop)
    // their retained winner on every render, so a connected trigger is current.
    if (!data || !tooltipTarget.isConnected || tooltipTarget.closest('[hidden]') || !pageActive) {
      hidePriceTooltip();
      return;
    }
    const {estimate, venue, snapshot: snap} = data;
    const row = snap === snapshot ? rows.get(venue.venue) : null;
    const age = ageSeconds(venue);
    const expired = row?.stale || estimate.status === 'stale' || venue.status === 'stale' || (age !== null && age > maxAge);
    if (renderedTooltipData !== data || renderedTooltipExpired !== expired) {
      const digits = priceDigits(snap);
      const rounded = (value, unit, missing) => supplied(value) ? `${Fmt.number(value, digits)} ${unit}` : missing;
      const roundTrip = isRoundTrip(snap.side);
      const heading = node('p', roundTrip
        ? `${labelFor(venue.identity)} · ${tradePhrase(snap.side, snap.hold_hours)} · ${sizeLabel(estimate.notional_usd)}`
        : `${labelFor(venue.identity)} · ${snap.side} · per 1 ${snap.asset}`, 'execution-tooltip-heading');
      heading.id = 'execution-price-tooltip-heading';
      const ageNode = node('p', null, 'execution-tooltip-age');
      ageNode.dataset.tooltipAge = 'true';
      ageNode.dataset.expired = String(Boolean(expired));
      const list = node('dl', null, 'execution-tooltip-list');
      if (roundTrip) {
        roundTripTooltipLines(list, estimate, venue, snap, rounded);
      } else {
        tooltipLine(list, `OB best ${snap.side === 'buy' ? 'ask' : 'bid'}`,
          nativeAndUsd(estimate.top_of_book_price, estimate.top_of_book_price_usd, venue, 'Unavailable', rounded, snap),
          nativeAndUsd(estimate.top_of_book_price, estimate.top_of_book_price_usd, venue, 'Unavailable', exact, snap));
        tooltipLine(list, 'Book slippage', bps(estimate.price_impact_bps), exact(estimate.price_impact_bps, 'bps', UNKNOWN));
        tooltipLine(list, 'Gross fill',
          nativeAndUsd(estimate.average_price, estimate.average_price_usd, venue, 'Unavailable', rounded, snap),
          nativeAndUsd(estimate.average_price, estimate.average_price_usd, venue, 'Unavailable', exact, snap));
        tooltipLine(list, 'Taker fee rate', feeRate(venue.fee?.taker_bps), exact(venue.fee?.taker_bps, 'bps', UNKNOWN));
        const missingEffectivePrice = !supplied(venue.fee?.taker_bps) ? 'Fee unknown — not ranked' : 'USD unavailable (FX unknown)';
        tooltipLine(list, 'Fee-adjusted price · USD',
          rounded(estimate.effective_price_usd, `USD per ${snap.asset}`, missingEffectivePrice),
          exact(estimate.effective_price_usd, `USD per ${snap.asset}`, missingEffectivePrice));
        if (holding(snap)) {
          const missingFunding = venue.funding?.unavailable ? `${venue.funding.unavailable} — not ranked` : 'Unavailable';
          tooltipLine(list, `Funding · ${holdLabels[String(snap.hold_hours)]}`,
            supplied(estimate.funding_bps) ? signedBps(estimate.funding_bps) : missingFunding,
            exact(estimate.funding_bps, 'bps', missingFunding));
          tooltipLine(list, 'After funding · USD',
            rounded(estimate.held_price_usd, `USD per ${snap.asset}`, missingFunding),
            exact(estimate.held_price_usd, `USD per ${snap.asset}`, missingFunding));
        }
      }
      tooltip.replaceChildren(heading, ageNode, list, node('p', `Rounded · select the ${roundTrip ? 'cost' : 'price'} for the full breakdown.`, 'execution-tooltip-help'));
      renderedTooltipData = data;
      renderedTooltipExpired = expired;
    }
    tooltip.querySelector('[data-tooltip-age]').textContent = expired
      ? `Expired${age === null ? '' : ` · ${Math.floor(age)}s old`} — last capture, not a current quote`
      : `Captured quote${age === null ? ' · Age unknown' : ` · ${Math.floor(age)}s old`} · expires after ${maxAge}s`;
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
  // A best price's breakdown opens as an overlay beneath the strip, one at a
  // time, so the strip keeps its height; Escape or a press elsewhere closes it.
  function closeBestDetail(view, restoreFocus) {
    if (!view.detail.open) return;
    const focused = view.detail.contains(document.activeElement);
    view.detail.open = false;
    if (restoreFocus && focused) view.price.focus();
  }
  function bindBestDetails() {
    for (const view of bestCards) {
      view.detail.addEventListener('toggle', () => {
        if (!view.detail.open) return;
        for (const other of bestCards) if (other !== view) other.detail.open = false;
      });
      view.breakdown.addEventListener('click', event => {
        if (event.target instanceof Element && event.target.closest('[data-best-close]')) closeBestDetail(view, true);
      });
    }
    document.addEventListener('keydown', event => {
      const view = event.key === 'Escape' && bestCards.find(item => item.detail.open);
      if (view) closeBestDetail(view, true);
    });
    document.addEventListener('pointerdown', event => {
      const view = bestCards.find(item => item.detail.open);
      if (view && !view.detail.contains(event.target)) closeBestDetail(view, false);
    });
  }
  function selectedFeeText(venueId, responseFee) {
    const schedule = schedules[venueId];
    const tier = schedule?.tiers?.find(item => item.id === feeChoices[venueId]);
    const rate = tier ? tier.taker_bps : responseFee?.taker_bps;
    return supplied(rate) && Number.isFinite(Number(rate)) ? `${feeRate(rate)} fee` : 'Fee unknown';
  }
  // Line 1: the venue's dot and display name, its product name the tooltip.
  // Line 2: fee and quote age, with [details] on the right. A hold's funding
  // adds a line to every row alike. The name waits for the first capture,
  // which carries the venue identity.
  function buildRow(venueId) {
    if (rows.has(venueId)) return rows.get(venueId);
    const schedule = schedules[venueId];
    const tr = node('tr');
    tr.dataset.venue = venueId;
    const venueCell = node('th', null, 'sticky-col');
    venueCell.scope = 'row';
    const identity = node('span', null, 'status-dot text-faint');
    identity.setAttribute('aria-hidden', 'true');
    const name = node('span', null, 'venue-name');
    name.append(skeleton('9ch'));
    if (schedule?.label) name.title = schedule.label;
    const heading = node('div', null, 'venue-heading');
    heading.append(identity, name);
    const age = node('span', null, 'execution-age');
    age.setAttribute('aria-live', 'off');
    const state = node('div', null, 'execution-row-state');
    state.hidden = true;
    const bookDetails = node('details');
    const bookSummary = node('summary', 'Details', 'bracket-btn');
    bookSummary.setAttribute('aria-label', 'Quote and availability details');
    bookDetails.append(bookSummary);
    const bookBody = node('div', null, 'execution-book-body');
    bookDetails.append(bookBody);
    bookDetails.hidden = true;
    const venueFee = node('span', selectedFeeText(venueId), 'execution-fee-rate');
    const funding = node('div', null, 'execution-row-funding');
    funding.hidden = true;
    const meta = node('div', null, 'venue-meta');
    const stats = node('div', null, 'meta-line execution-row-stats');
    stats.append(venueFee, age);
    meta.append(stats, bookDetails);
    venueCell.append(heading, state, meta, funding);
    const feeControl = node('label', null, 'field execution-fee-control');
    feeControl.append(node('span', schedule?.label || venueId, 'field-label'));
    const shell = node('span', null, 'select-shell');
    const select = node('select', null, 'select select-polished execution-fee-select');
    select.dataset.feeVenue = venueId;
    select.setAttribute('aria-label', `${schedule?.label || venueId} native VIP or account type`);
    for (const tier of schedule?.tiers || []) {
      const option = node('option', `${tier.label} · ${supplied(tier.taker_bps) ? Fmt.feeBps(tier.taker_bps) : 'Fee unknown'}`);
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
        capture();
        loadHistory();
      });
    } else {
      select.append(node('option', 'Public fee unavailable'));
      select.disabled = true;
    }
    shell.append(select);
    const feeNote = node('div', selectedFeeText(venueId), 'execution-fee-rate');
    feeControl.append(shell, feeNote);
    select.title = select.selectedOptions[0]?.textContent || '';
    $('execution-fee-controls').append(feeControl);
    const cells = notionals.map(notional => {
      const cell = node('td', null, 'num execution-price-cell');
      cell.dataset.notional = notional;
      return cell;
    });
    tr.append(venueCell, ...cells);
    tbody.append(tr);
    const row = {tr, name, named: false, identity, age, state, bookSummary, bookDetails, bookBody, select, feeControl, feeRate: feeNote, venueFee, funding, cells, venue: null, prices: [], stale: false};
    rows.set(venueId, row);
    return row;
  }
  function populateFeeSources() {
    const container = $('execution-fee-sources');
    for (const [venueId, schedule] of Object.entries(schedules)) {
      const details = node('details', null, 'disclosure execution-fee-source');
      details.dataset.venue = venueId;
      details.append(node('summary', `${schedule.label} · checked ${schedule.checked_at || 'date unavailable'}`));
      const body = node('div', null, 'disclosure-body');
      body.append(sourceLink(schedule.source_url, 'Official fee schedule'));
      const list = node('ul', null, 'execution-fee-notes');
      for (const note of schedule.notes || []) list.append(node('li', note));
      body.append(list);
      const tiers = node('ul', null, 'execution-fee-notes');
      for (const tier of schedule.tiers || []) {
        tiers.append(node('li', `${tier.label}: ${supplied(tier.taker_bps) ? `${Fmt.feeBps(tier.taker_bps)} taker` : UNKNOWN}${tier.id === schedule.default_tier ? ' · default' : ''}`));
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
  // The value a comparison ranks: the price per unit (after funding when
  // held), or a round trip's whole-trade cost.
  function rankedValue(estimate, snap = snapshot) {
    if (isRoundTrip(snap?.side)) return estimate?.total_cost_usd;
    return holding(snap) ? estimate?.held_price_usd : estimate?.effective_price_usd;
  }
  // A venue's quote can win a size; staleness is judged separately so an
  // expired winner can stay on screen, marked, until a new capture lands.
  function bestCandidate(row, index) {
    const venue = row.venue;
    const estimate = row.prices[index];
    return Boolean(venue && estimate?.best && estimate.complete && estimate.status === 'ok' && venue.status === 'ok'
      && supplied(venue.fee?.taker_bps) && supplied(rankedValue(estimate)) && ageSeconds(venue) !== null);
  }
  // Rows run best-first at one size: cheapest to buy, highest to sell, the
  // cheapest round trip, by the same value the server ranks. Venues with no
  // ranked value at that size (shallow book, above limit, unknown fee,
  // unavailable) follow in name order. A size header picks the size; the
  // choice is remembered.
  const sortFilters = Overseer.filterState('execution');
  const sortButtons = [...document.querySelectorAll('[data-execution-sort]')];
  let sortIndex = Math.max(0, notionals.indexOf(sortFilters.get('sort')));
  function sortValue(row) {
    const estimate = row.prices[sortIndex];
    if (!row.venue || estimate?.status !== 'ok' || !estimate.complete) return null;
    const value = Number(rankedValue(estimate));
    return supplied(rankedValue(estimate)) && Number.isFinite(value) ? value : null;
  }
  function sortRows() {
    const highestFirst = !isRoundTrip(snapshot?.side ?? trade.value) && (snapshot?.side ?? trade.value) === 'sell';
    sortButtons.forEach((button, index) => {
      const header = button.closest('th');
      if (index === sortIndex) header.setAttribute('aria-sort', highestFirst ? 'descending' : 'ascending');
      else header.removeAttribute('aria-sort');
      button.setAttribute('aria-label', `Sort venues by ${sizeLabel(notionals[index])}, best first`);
    });
    if (!snapshot) return;
    const ordered = [...rows.values()].filter(row => !row.tr.hidden).sort((a, b) => {
      const first = sortValue(a);
      const second = sortValue(b);
      if (first !== null && second !== null && first !== second) return highestFirst ? second - first : first - second;
      if ((first === null) !== (second === null)) return first === null ? 1 : -1;
      return a.name.textContent.localeCompare(b.name.textContent);
    });
    tbody.append(...ordered.map(row => row.tr));
  }
  function updateRowAge(row) {
    const venue = row.venue;
    if (!venue) return;
    const age = ageSeconds(venue);
    const stale = venue.status === 'stale' || row.prices.some(price => price?.status === 'stale') || (age !== null && age > maxAge);
    row.stale = stale;
    row.tr.dataset.stale = String(stale);
    const basis = venue.timestamp_basis === 'venue' ? 'venue' : venue.timestamp_basis === 'fetch' ? 'receipt' : 'unknown';
    setText(row.age, age === null ? 'Age unknown' : stale ? `Expired ${Math.floor(age)}s` : `${Math.floor(age)}s`);
    row.age.title = `Age anchored to server completion, then advanced locally; timestamp basis: ${basis}. Source: ${venue.captured_at || 'unknown'}. Received: ${venue.fetched_at || 'unknown'}.`;
    setText(row.state, label(venue.status));
    row.state.title = venue.error || label(venue.status);
    row.state.hidden = ['ok', 'stale', 'fee_unknown', 'funding_unknown'].includes(venue.status) && !venue.error;
    row.cells.forEach((cell, index) => {
      const best = bestCandidate(row, index) && !stale;
      cell.dataset.best = String(best);
      cell.dataset.stale = String(stale);
      cell.querySelectorAll('[data-price-freshness]').forEach(reading => {
        setText(reading, stale ? 'Expired — last capture, not a current quote' : 'Captured quote — refresh before trading');
      });
      const bestMark = cell.querySelector('[data-best-mark]');
      if (bestMark) bestMark.hidden = !best;
      const summary = cell.querySelector('.execution-price-detail > summary');
      if (summary) summary.setAttribute('aria-label', `${best ? 'Best. ' : ''}${summary.dataset.label}`);
    });
  }
  function comparisonKey() {
    return JSON.stringify([asset.value, trade.value, hold.value, feeChoices]);
  }
  function setCardState(view, state) {
    view.card.dataset.state = state;
    view.rendered = state;
  }
  function renderBestCard(view) {
    const {estimate, venue, names, labels, snapshot: snap} = view.model;
    const age = ageSeconds(venue);
    const expired = age === null || age > maxAge;
    const roundTrip = isRoundTrip(snap.side);
    const value = rankedValue(estimate, snap);
    setCardState(view, expired ? 'expired' : 'ready');
    setText(view.price, roundTrip ? Fmt.usd(value) : usdPrice(value, priceDigits(snap)));
    setText(view.venue, names.join(' / '));
    view.venue.title = labels.join(' / ');
    view.dot.hidden = false;
    view.dot.style.color = colorFor(venue.identity);
    view.price.removeAttribute('aria-disabled');
    view.price.removeAttribute('tabindex');
    const stale = expired ? '. Expired quote from the last capture' : '';
    const funded = holding(snap) ? ' and funding' : '';
    view.price.setAttribute('aria-label', roundTrip
      ? `${names[0]}: ${exact(value, 'USD')} to ${tradePhrase(snap.side, snap.hold_hours)} at this size, after taker fees${funded}${stale}. Expand for the cost breakdown.`
      : `${names[0]}: ${exact(value, 'USD')} per ${snap.asset}, after taker fees${funded}${stale}. Expand for price and fee breakdown.`);
    registerPriceTrigger(view.price, estimate, venue, snap);
    if (view.estimate !== estimate) {
      const [list, freshness, components, diagnostics] = roundTrip ? roundTripBreakdown(estimate, venue) : priceBreakdown(estimate, venue);
      const head = node('div', null, 'execution-breakdown-head');
      const close = node('button', 'Close', 'bracket-btn');
      close.type = 'button';
      close.dataset.bestClose = 'true';
      close.setAttribute('aria-label', 'Close breakdown');
      head.append(node('p', `${sizeLabel(view.card.dataset.bestNotional)} trade · ${names[0]}`, 'execution-breakdown-title'), close);
      view.breakdown.replaceChildren(head, freshness, list, components, diagnostics);
      view.estimate = estimate;
    }
    view.breakdown.querySelectorAll('[data-price-freshness]').forEach(reading => {
      setText(reading, expired ? 'Expired — last capture, not a current quote' : 'Captured quote — refresh before trading');
    });
    view.badge.hidden = !expired;
    if (expired) {
      setText(view.badge, age === null ? 'Expired' : `Expired ${Math.floor(age)}s`);
      view.badge.title = `Quote expired after ${maxAge}s${age === null ? '' : `; captured ${Math.floor(age)}s ago`}. Refresh for a current price.`;
    }
    const cost = allInCostDisplay(estimate, snap);
    view.cost.hidden = false;
    setText(view.cost, cost.text);
    view.cost.title = cost.explanation;
    view.note.hidden = names.length < 2;
    setText(view.note, names.length > 1 ? `Tied best · breakdown: ${names[0]}` : '');
  }
  function renderEmptyCard(view) {
    const state = status.dataset.state === 'loading' ? 'loading' : status.dataset.state === 'error' ? 'error' : 'unavailable';
    if (view.rendered === state) return;
    setCardState(view, state);
    unregisterPriceTrigger(view.price);
    view.price.removeAttribute('aria-label');
    view.price.setAttribute('aria-disabled', 'true');
    view.price.setAttribute('tabindex', '-1');
    view.detail.open = false;
    if (view.estimate) view.breakdown.replaceChildren();
    view.estimate = null;
    view.badge.hidden = true;
    view.dot.hidden = true;
    view.venue.removeAttribute('title');
    view.cost.hidden = true;
    view.cost.textContent = '';
    view.cost.removeAttribute('title');
    if (state === 'loading') {
      view.price.replaceChildren(skeleton('9ch'));
      view.venue.replaceChildren(skeleton('12ch'));
      view.note.hidden = true;
      return;
    }
    view.price.textContent = '—';
    view.venue.textContent = state === 'error' ? 'Prices unavailable' : 'No full quote';
    view.note.hidden = state === 'error';
    view.note.textContent = state === 'error' ? '' : 'See venue availability below';
  }
  function renderBestSummary() {
    const held = Number(hold.value) > 0;
    if (isRoundTrip(trade.value)) {
      const phrase = tradePhrase(trade.value, hold.value);
      setText($('execution-best-help'), `Lowest cost to ${phrase}, after taker fees${held ? ' and funding' : ''}`);
      setText($('execution-price-heading'), `Cost in USD to ${phrase}, after taker fees${held ? ' and funding' : ''}`);
      setText($('execution-cost-legend'), 'Beneath: the same cost in bps of the trade size');
    } else {
      const buying = trade.value === 'buy';
      const funded = held ? ` and ${holdLabels[hold.value]} of funding` : '';
      setText($('execution-best-help'), `${buying ? 'Lowest buy' : 'Highest sell'} price after taker fees${funded}`);
      setText($('execution-price-heading'), `${buying ? 'Buy' : 'Sell'} price per ${asset.value || 'unit'} in USD after taker fees${funded}`);
      setText($('execution-cost-legend'), `Beneath: ${held ? 'cost' : 'one-way cost'} in bps (taker fee + book slippage${held ? ' + funding' : ''})`);
    }
    let custom = 0;
    for (const [venueId, row] of rows) {
      if (!row.tr.hidden && feeChoices[venueId] !== schedules[venueId]?.default_tier) custom++;
    }
    setText($('execution-fee-summary'), custom ? `${custom} modified ${custom === 1 ? 'tier' : 'tiers'}` : 'Base tiers');
    const key = comparisonKey();
    bestCards.forEach((view, index) => {
      if (snapshot) {
        const winners = [...rows.values()].filter(row => !row.tr.hidden && bestCandidate(row, index));
        view.model = winners.length ? {
          key, snapshot, names: winners.map(row => labelFor(row.venue.identity)), labels: winners.map(row => row.venue.label),
          estimate: winners[0].prices[index], venue: winners[0].venue,
        } : null;
      } else if (view.model?.key !== key) {
        // A different asset, side or fee tier makes the retained winner irrelevant.
        view.model = null;
      }
      if (view.model) renderBestCard(view);
      else renderEmptyCard(view);
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
    const held = holding(snapshot);
    const {value: costValue, known: costKnown} = allInCostDisplay(estimate);
    const nativeUnit = `${venue.quote_currency || 'native quote'} per ${snapshot.asset}`;
    const usdUnit = `USD per ${snapshot.asset}`;
    const list = node('dl', null, 'execution-detail-list');
    detailLine(list, `Gross fill per ${snapshot.asset} · native`, exact(estimate.average_price, nativeUnit));
    detailLine(list, `Gross fill per ${snapshot.asset} · USD`, exact(estimate.average_price_usd, usdUnit, fxUnknown ? 'FX unknown — not ranked' : 'Unavailable'));
    detailLine(list, `Fee-adjusted fill per ${snapshot.asset} · USD`, exact(estimate.effective_price_usd, usdUnit, feeUnknown ? 'Fee unknown — not ranked' : fxUnknown ? 'FX unknown — not ranked' : 'Unavailable'));
    if (held) {
      const missingFunding = venue.funding?.unavailable ? `${venue.funding.unavailable} — not ranked` : 'Unavailable';
      detailLine(list, `Funding while held · ${holdLabels[String(snapshot.hold_hours)]}`, exact(estimate.funding_bps, 'bps', missingFunding));
      detailLine(list, 'Funding on this size · USD', exact(estimate.funding_usd, 'USD', missingFunding));
      detailLine(list, 'Price after fees and funding · USD', exact(estimate.held_price_usd, usdUnit, missingFunding));
    }
    detailLine(list, `OB best ${snapshot.side === 'buy' ? 'ask' : 'bid'} · native`, exact(estimate.top_of_book_price, nativeUnit));
    detailLine(list, `OB best ${snapshot.side === 'buy' ? 'ask' : 'bid'} · USD`, exact(estimate.top_of_book_price_usd, usdUnit, fxUnknown ? 'FX unknown' : 'Unavailable'));
    detailLine(list, `Taker fee per ${snapshot.asset} · native · ${snapshot.side === 'buy' ? 'added' : 'deducted'}`, signedPerCoin(estimate.fee_per_coin, venue.quote_currency || 'native quote', feeUnknown ? UNKNOWN : 'Unavailable'));
    detailLine(list, `Taker fee per ${snapshot.asset} · USD`, signedPerCoin(estimate.fee_per_coin_usd, 'USD', feeUnknown ? UNKNOWN : fxUnknown ? 'FX unknown' : 'Unavailable'));
    const freshness = node('p', rows.get(venue.venue)?.stale ? 'Expired — last capture, not a current quote' : 'Captured quote — refresh before trading', 'execution-price-state');
    freshness.dataset.priceFreshness = 'true';
    const components = node('dl', null, 'execution-detail-list execution-cost-components');
    const feeComponent = feeUnknown ? 'Fee unknown' : `${bps(venue.fee?.taker_bps)} fee`;
    const slippageComponent = `${bps(estimate.price_impact_bps)} slippage`;
    const funding = Number(estimate.funding_bps);
    const fundingComponent = !held ? '' : supplied(estimate.funding_bps)
      ? ` ${funding < 0 ? '−' : '+'} ${bps(String(Math.abs(funding)))} funding` : ' + unknown funding';
    const equation = `${feeComponent} + ${slippageComponent}${fundingComponent} = ${costKnown && !feeUnknown ? bps(costValue) + ' total' : `total ${UNKNOWN.toLowerCase()}`}`;
    detailLine(components, held ? 'Cost · fee + book slippage + funding' : 'One-way cost · fee + book slippage', equation);
    detailLine(components, 'Exact taker fee rate', exact(venue.fee?.taker_bps, 'bps', UNKNOWN));
    detailLine(components, `Exact book slippage vs venue's ${snapshot.side === 'buy' ? 'best ask' : 'best bid'}`, exact(estimate.price_impact_bps, 'bps'));
    detailLine(components, 'Exact total cost', costKnown && !feeUnknown ? exact(costValue, 'bps') : UNKNOWN);
    detailLine(components, 'Whole-order one-way taker fee · USD', exact(estimate.fee_usd, 'USD', feeUnknown ? UNKNOWN : 'Unavailable'));
    const diagnostics = node('dl', null, 'execution-detail-list');
    detailLine(diagnostics, 'Common unrounded base quantity', exact(estimate.quantity, snapshot.asset));
    detailLine(diagnostics, 'Visible filled quantity', exact(estimate.filled_quantity, snapshot.asset));
    detailLine(diagnostics, 'Native fee assumption', `${venue.fee?.tier_label || 'Unknown'} · ${exact(venue.fee?.taker_bps, 'bps', UNKNOWN)}`);
    if (held) detailLine(diagnostics, 'Funding basis', fundingBasisText(venue.funding));
    return [list, freshness, components, diagnostics];
  }
  function roundTripBreakdown(estimate, venue) {
    const held = holding(snapshot);
    const nativeUnit = `${venue.quote_currency || 'native quote'} per ${snapshot.asset}`;
    const usdUnit = `USD per ${snapshot.asset}`;
    const missingFunding = venue.funding?.unavailable ? `${venue.funding.unavailable} — not ranked` : 'Unavailable';
    const legs = node('dl', null, 'execution-detail-list');
    for (const [name, leg] of [['Buy', estimate.buy], ['Sell', estimate.sell]]) {
      detailLine(legs, `${name} gross fill per ${snapshot.asset} · native`, exact(leg?.average_price, nativeUnit));
      detailLine(legs, `${name} fee-adjusted fill per ${snapshot.asset} · USD`, exact(leg?.effective_price_usd, usdUnit, 'Unavailable'));
      detailLine(legs, `${name} taker fee, whole order · USD`, exact(leg?.fee_usd, 'USD', UNKNOWN));
    }
    const freshness = node('p', rows.get(venue.venue)?.stale ? 'Expired — last capture, not a current quote' : 'Captured quote — refresh before trading', 'execution-price-state');
    freshness.dataset.priceFreshness = 'true';
    const components = node('dl', null, 'execution-detail-list execution-cost-components');
    detailLine(components, 'Spread + book slippage · USD', exact(estimate.spread_slippage_usd, 'USD', 'USD unavailable (FX unknown)'));
    detailLine(components, 'Taker fees, both legs · USD', exact(estimate.fees_usd, 'USD', UNKNOWN));
    if (held) detailLine(components, `Funding while held · ${holdLabels[String(snapshot.hold_hours)]} · USD`, exact(estimate.funding_usd, 'USD', missingFunding));
    detailLine(components, 'Total cost · USD', exact(estimate.total_cost_usd, 'USD', UNKNOWN));
    detailLine(components, 'Total cost · bps of the trade size', exact(estimate.total_cost_bps, 'bps', UNKNOWN));
    const diagnostics = node('dl', null, 'execution-detail-list');
    detailLine(diagnostics, 'Common unrounded base quantity', exact(estimate.quantity, snapshot.asset));
    detailLine(diagnostics, 'Native fee assumption', `${venue.fee?.tier_label || 'Unknown'} · ${exact(venue.fee?.taker_bps, 'bps', UNKNOWN)} each way`);
    if (held) detailLine(diagnostics, 'Funding basis', fundingBasisText(venue.funding));
    return [legs, freshness, components, diagnostics];
  }
  // What a cell shows for a buy or sell: the price per unit, labelled when a
  // fee, FX rate or funding is unknown.
  function oneWayValue(estimate, venue) {
    if (!estimate.complete) return null;
    const feeUnknown = !supplied(venue.fee?.taker_bps) || estimate.status === 'fee_unknown';
    const fxUnknown = venue.status === 'fx_missing' || estimate.status === 'fx_missing';
    const held = holding(snapshot);
    let price = held ? estimate.held_price_usd : estimate.effective_price_usd;
    let unit = 'USD';
    let label = held ? 'After taker fee and funding' : 'After taker fee';
    let qualifier = null;
    if (fxUnknown) {
      price = estimate.average_price;
      unit = venue.quote_currency || 'native quote';
      label = qualifier = 'Gross · FX unknown';
    } else if (feeUnknown) {
      price = estimate.average_price_usd;
      label = qualifier = 'Before fees';
    } else if (estimate.status === 'funding_unknown') {
      // The cost line beneath already says "before funding".
      price = estimate.effective_price_usd;
      label = 'Before funding';
    }
    if (!supplied(price)) return null;
    return {
      text: unit === 'USD' ? usdPrice(price, snapshotDigits) : `${Fmt.number(price, snapshotDigits)} ${unit}`,
      exact: `${exact(price, unit)} per ${snapshot.asset}`, label, qualifier,
      note: fxUnknown && feeUnknown ? 'Fee unknown · before fees' : null,
    };
  }
  // A round trip shows its whole-trade cost in USD, or the part that is known;
  // the cost line beneath names what a partial amount leaves out.
  function roundTripValue(estimate, venue) {
    const known = [[estimate.total_cost_usd, 'Round-trip cost'], [estimate.trading_cost_usd, 'Before funding'],
      [estimate.spread_slippage_usd, holding(snapshot) ? 'Before fees and funding' : 'Before fees']]
      .find(([value]) => supplied(value));
    if (known) {
      const [value, label] = known;
      return {text: Fmt.usd(value), exact: `${exact(value, 'USD')} for the whole trade`, label, qualifier: null, note: null};
    }
    if (!supplied(estimate.spread_slippage)) return null;
    const unit = venue.quote_currency || 'native quote';
    return {text: `${Fmt.number(estimate.spread_slippage, 2)} ${unit}`, exact: `${exact(estimate.spread_slippage, unit)} for the whole trade`,
      label: 'Gross · FX unknown', qualifier: 'Gross · FX unknown', note: null};
  }
  function renderPrice(cell, estimate, venue) {
    cell.replaceChildren();
    cell.removeAttribute('title');
    cell.dataset.best = 'false';
    cell.dataset.status = estimate?.status || 'unavailable';
    if (!estimate) {
      cell.append(node('div', 'Unavailable', 'execution-price-state'));
      return;
    }
    // The winner is marked inside the cost line, so it never adds a line.
    const bestMark = node('span', 'best', 'execution-best-mark');
    bestMark.dataset.bestMark = 'true';
    bestMark.hidden = true;
    const roundTrip = isRoundTrip(snapshot.side);
    const rejected = ['insufficient_depth', 'order_limit', 'no_reference', 'invalid_book', 'invalid_timestamp', 'unavailable'].includes(estimate.status);
    const shown = rejected ? null : roundTrip ? roundTripValue(estimate, venue) : oneWayValue(estimate, venue);
    if (shown) {
      const details = node('details', null, 'execution-price-detail');
      const summary = node('summary');
      summary.append(node('span', shown.text, 'execution-price-value'));
      const {text: costText, explanation: costExplanation} = allInCostDisplay(estimate);
      const cost = node('span', null, 'execution-all-in-cost');
      cost.append(bestMark, costText);
      summary.append(cost);
      if (shown.qualifier) summary.append(node('span', shown.qualifier, 'execution-price-label'));
      summary.dataset.label = `${shown.label}: ${shown.exact}. ${costExplanation} Expand for the ${roundTrip ? 'cost' : 'price and fee'} breakdown.`;
      summary.setAttribute('aria-label', summary.dataset.label);
      registerPriceTrigger(summary, estimate, venue);
      details.append(summary, ...(roundTrip ? roundTripBreakdown(estimate, venue) : priceBreakdown(estimate, venue)));
      cell.append(details);
      if (shown.note) cell.append(node('div', shown.note, 'execution-price-state'));
    } else {
      cell.append(node('div', label(estimate.status === 'ok' ? 'unavailable' : estimate.status), 'execution-price-state'));
      if (estimate.status === 'insufficient_depth') {
        // A round trip is as shallow as its shorter side.
        const book = roundTrip ? estimate.short_side : snapshot.side;
        const leg = roundTrip ? estimate[book] || estimate : estimate;
        const share = Fmt.number(leg.fill_percent, 1);
        const fill = node('div', share === '—' ? 'Partly captured' : `${share}% captured`, 'execution-fill');
        const depth = book === 'buy' ? venue.depth?.ask_levels : venue.depth?.bid_levels;
        const explanation = `${supplied(depth) ? `${depth} captured ${book === 'buy' ? 'ask' : 'bid'} levels cover ` : 'Captured depth covers '}${exact(leg.filled_quantity, snapshot.asset)} of ${exact(leg.quantity, snapshot.asset)} requested. This is a snapshot coverage limit, not a measure of the venue's full liquidity. No full-size price is extrapolated.`;
        fill.title = explanation;
        cell.title = explanation;
        cell.append(fill);
      }
      if (estimate.status === 'order_limit') cell.title = 'Exceeds the published market-order maximum; expand Details for the cap.';
    }
  }
  function renderReference() {
    $('execution-reference').textContent = supplied(snapshot.reference_price_usd)
      ? `${snapshot.asset} reference · ${usdPrice(snapshot.reference_price_usd, snapshotDigits)}`
      : `${snapshot.asset}: no fresh common USD reference; full-size comparisons unavailable.`;
    $('execution-reference').title = `Exact reference USD midpoint median: ${exact(snapshot.reference_price_usd, 'USD')}. Reference-USD entry comparison, not a cash purchase or conversion commitment.`;
    for (const header of document.querySelectorAll('[data-execution-notional]')) {
      const index = notionals.indexOf(header.dataset.executionNotional);
      const quantity = snapshot.quantities?.[index];
      const target = header.querySelector('.execution-quantity');
      target.textContent = supplied(quantity) ? `${quantityText(quantity)} ${snapshot.asset}` : '—';
      target.title = supplied(quantity) ? `Common unrounded base quantity: ${exact(quantity, snapshot.asset)}; reference notional ${exact(header.dataset.executionNotional, 'USD')}, not an all-in cash budget.` : 'No fresh common USD reference price.';
    }
    const fx = snapshot.fx || {};
    const rates = Object.entries(fx.rates || {}).filter(([currency]) => currency !== 'USD')
      .map(([currency, rate]) => `${currency} ${Fmt.number(rate, 4)}`).join(' · ');
    const fxNode = $('execution-fx');
    const fxTime = fx.ts && Number.isFinite(Date.parse(fx.ts)) ? Fmt.time(fx.ts) : null;
    fxNode.title = `Exact USD reference rates: ${Object.entries(fx.rates || {}).map(([currency, rate]) => `${currency} = ${exact(rate, 'USD')}`).join('; ')}. Checked: ${fx.ts || 'unavailable'}. ${fx.error || 'No stablecoin peg assumed; not an executed conversion.'}`;
    fxNode.replaceChildren(node('span', `USD FX · ${rates || 'stablecoin rates unavailable'}${fxTime ? ' · ' + fxTime : ''}${fx.error ? ' · incomplete' : ''} · `));
    const source = sourceLink(fx.source_url, 'Source');
    source.setAttribute('aria-label', fx.source || 'Official reference FX source');
    fxNode.append(source);
  }
  // Status bar: a short value, read with the comparison first; a state with a
  // tone; the asset and trade in the meta slot. A failure's detail is spoken
  // and kept as the value's tooltip.
  function setStatusState(text, tone) {
    statusState.hidden = !text;
    setText(statusState.querySelector('[data-status-text]'), text);
    if (tone) statusState.dataset.tone = tone;
    else delete statusState.dataset.tone;
  }
  function setStatus(state, value, {context = '', lead = '', note = '', stateText = '', tone = ''} = {}) {
    status.dataset.state = state;
    const spoken = [];
    if (context) spoken.push(node('span', `${context}: `, 'sr-only'));
    // Phones drop the lead ("8/8 captured ") to keep the bar on one line.
    if (lead) spoken.push(node('span', lead, 'status-bar-wide'));
    spoken.push(document.createTextNode(value));
    if (note) spoken.push(node('span', `. ${note}`, 'sr-only'));
    status.replaceChildren(...spoken);
    if (note) status.title = note;
    else status.removeAttribute('title');
    setText(statusContext, context);
    statusContext.hidden = !context;
    setStatusState(stateText, tone);
  }
  // The notice is for real problems only (failed or partial capture, unusable
  // comparison); routine expiry shows in the status bar and on the cards.
  function setWarning(text) {
    warning.hidden = !text;
    if (text) setText(warning, text);
  }
  function renderSnapshot(data) {
    clearPriceTooltip();
    snapshot = data;
    snapshotDigits = priceDigits(data);
    completedAt = Date.parse(data.completed_at);
    receivedAt = performance.now();
    renderReference();
    const visible = new Set();
    for (const venue of data.venues) {
      visible.add(venue.venue);
      const row = buildRow(venue.venue);
      row.tr.hidden = false;
      row.venue = venue;
      row.prices = notionals.map(notional => venue.estimates?.find(estimate => estimate.notional_usd === notional));
      row.name.textContent = labelFor(venue.identity);
      row.name.title = venue.label;
      row.named = true;
      row.bookSummary.setAttribute('aria-label', `${labelFor(venue.identity)} quote and availability details`);
      row.identity.classList.remove('text-faint');
      row.identity.style.color = colorFor(venue.identity);
      row.feeRate.textContent = selectedFeeText(venue.venue, venue.fee);
      row.venueFee.textContent = selectedFeeText(venue.venue, venue.fee);
      row.funding.hidden = !venue.funding;
      if (venue.funding) {
        // The hold's cost, on its own line; the rate it comes from is in the tooltip.
        setText(row.funding, venue.funding.unavailable ? 'Funding unknown' : `${signedBps(venue.funding.cost_bps)} funding`);
        row.funding.title = `${fundingBasisText(venue.funding)}. Over ${holdLabels[String(data.hold_hours)]}, positive is paid by the position and negative is received.`;
        row.funding.classList.toggle('text-phosphor', Boolean(venue.funding.unavailable));
      }
      renderBookDetails(row, venue);
      row.cells.forEach((cell, index) => renderPrice(cell, row.prices[index], venue));
      updateRowAge(row);
    }
    for (const [venueId, row] of rows) {
      row.tr.hidden = !visible.has(venueId);
      row.feeControl.hidden = row.tr.hidden;
    }
    sortRows();
    if (!data.venues.length) {
      const empty = node('tr', null, 'execution-empty-row');
      const cell = node('td', `No native perpetual venues list ${data.asset}. Choose another asset.`, 'empty-cell');
      cell.colSpan = notionals.length + 1;
      empty.append(cell);
      tbody.append(empty);
    }
    const count = data.venues.filter(venue => supplied(venue.fetched_at)).length;
    setStatus('ready', Fmt.time(data.completed_at), {
      lead: `${count}/${data.venues.length} captured `,
      context: `${data.asset} ${tradePhrase(data.side, data.hold_hours)}`, stateText: 'Fresh', tone: 'fresh',
    });
    const missing = data.venues.length - count;
    setWarning(!data.venues.length ? ''
      : !supplied(data.reference_price_usd) ? `No fresh common USD reference for ${data.asset}; full-size prices are not compared. Retry, or choose another asset.`
      : missing ? `${missing} of ${data.venues.length} venues returned no book in this capture; their rows show why.` : '');
    pageState.ready();
    renderBestSummary();
  }
  function clearQuotes() {
    clearPriceTooltip();
    snapshot = null;
    setWarning('');
    for (const empty of tbody.querySelectorAll('.execution-empty-row')) empty.remove();
    $('execution-reference').textContent = `${asset.value || 'Asset'} reference: awaiting capture.`;
    $('execution-reference').removeAttribute('title');
    $('execution-fx').textContent = 'USD reference FX: awaiting capture.';
    $('execution-fx').removeAttribute('title');
    document.querySelectorAll('.execution-quantity').forEach(target => {
      target.replaceChildren(skeleton('8ch'));
      target.removeAttribute('title');
    });
    for (const row of rows.values()) {
      row.venue = null;
      row.prices = [];
      row.stale = false;
      row.tr.hidden = false;
      row.tr.dataset.stale = 'false';
      row.age.replaceChildren(skeleton('7ch'));
      row.age.removeAttribute('title');
      row.state.hidden = true;
      row.state.removeAttribute('title');
      row.venueFee.textContent = selectedFeeText(row.tr.dataset.venue);
      row.funding.hidden = true;
      row.bookDetails.hidden = true;
      for (const cell of row.cells) {
        cell.replaceChildren(skeleton('10ch'), node('br'), skeleton('7ch'));
        cell.removeAttribute('title');
        cell.dataset.best = 'false';
        cell.dataset.stale = 'false';
        cell.dataset.status = 'loading';
      }
    }
    setStatus('loading', 'Fetching…', {context: `${asset.value} ${tradePhrase(trade.value, hold.value)}`});
    renderBestSummary();
  }
  // Failed or interrupted captures: the table shows no estimate, the best
  // cards keep any retained (expired) winner and only the error offers a retry.
  function showFailure(detail, rowState) {
    clearQuotes();
    setStatus('error', 'Unavailable', {context: `${asset.value} ${tradePhrase(trade.value, hold.value)}`, note: detail, stateText: rowState, tone: 'error'});
    setWarning(`${rowState}: ${detail}. Prices below are not current; Retry, or wait for the next automatic refresh.`);
    pageState.error(`${asset.value} prices`, () => capture());
    let shown = 0;
    for (const row of rows.values()) {
      // A venue never captured has no display name yet, so its row stays out.
      row.tr.hidden = !row.named;
      if (row.named) shown++;
      row.state.textContent = rowState;
      row.state.hidden = false;
      row.age.textContent = '—';
      for (const cell of row.cells) {
        cell.replaceChildren(node('span', '—', 'execution-price-state'));
        cell.dataset.status = 'unavailable';
      }
    }
    if (!shown) {
      const empty = node('tr', null, 'execution-empty-row');
      const cell = node('td', `${rowState}: no prices captured.`, 'empty-cell');
      cell.colSpan = notionals.length + 1;
      empty.append(cell);
      tbody.append(empty);
    }
    renderBestSummary();
  }
  function pageVisible() {
    return pageActive && !document.hidden;
  }
  function updateRefreshCadence() {
    if (!asset.options.length) {
      refreshCadence.textContent = 'Automatic refresh unavailable';
    } else if (!pageVisible()) {
      refreshCadence.textContent = 'Auto-refresh paused while this page is hidden';
    } else {
      const remaining = Math.max(0, Math.ceil((nextCaptureAt - Date.now()) / 1000));
      refreshCadence.textContent = `Auto-refresh every minute · next in ${remaining}s`;
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
    let captured = 0;
    let allStale = true;
    if (snapshot) {
      for (const row of rows.values()) {
        if (row.tr.hidden) continue;
        updateRowAge(row);
        if (row.venue?.captured_at) {
          captured++;
          allStale &&= row.stale;
        }
      }
    }
    // Runs without a snapshot too, so retained winners keep ticking.
    renderBestSummary();
    if (tooltipTarget) updatePriceTooltip();
    if (captured && allStale && status.dataset.state === 'ready') {
      status.dataset.state = 'stale';
      setStatusState('Expired', 'warning');
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
        capture();
        return;
      }
      skipElapsedRefreshes(now);
    }
    scheduleRefreshTimer();
  }
  // One request per page at a time. The server keeps capturing after a fetch
  // is abandoned, so overlapping requests from quick setting changes would
  // pile up there and can exhaust its two capture slots. A change made while
  // a request runs is captured once that request settles; the stale result
  // is dropped, and several changes become one capture.
  let captureQueued = false;
  async function capture() {
    clearPriceTooltip();
    if (!pageVisible() || !asset.options.length) return;
    const current = ++generation;
    nextCaptureAt = Date.now() + refreshIntervalMs;
    clearQuotes();
    table.setAttribute('aria-busy', 'true');
    refresh.setAttribute('aria-disabled', 'true');
    scheduleRefreshTimer();
    if (controller) {
      captureQueued = true;
      return;
    }
    controller = new AbortController();
    const signal = controller.signal;
    const request = {asset: asset.value, side: trade.value, hold_hours: Number(hold.value), fees: {...feeChoices}};
    try {
      const response = await fetch(config.endpoint, {
        method: 'POST', headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
        body: JSON.stringify(request), signal, cache: 'no-store', credentials: 'same-origin',
      });
      const data = await response.json();
      if (current !== generation || signal.aborted) return;
      if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : `Snapshot request failed (${response.status}).`);
      if (data.asset !== request.asset || data.side !== request.side || data.hold_hours !== request.hold_hours
          || !Array.isArray(data.venues) || !Number.isFinite(Date.parse(data.completed_at))) {
        throw new Error('Snapshot response did not match the requested comparison.');
      }
      renderSnapshot(data);
    } catch (error) {
      if (current !== generation || signal.aborted) return;
      showFailure(error instanceof SyntaxError ? 'Price service returned an unreadable response' : (error.message || 'Price request failed').replace(/\.$/, ''), 'Request failed');
    } finally {
      if (!signal.aborted) {
        controller = null;
        if (captureQueued) {
          captureQueued = false;
          // If the page is hidden by now, the refresh clock captures on return.
          nextCaptureAt = Date.now();
          capture();
        } else if (current === generation) {
          table.setAttribute('aria-busy', 'false');
          refresh.removeAttribute('aria-disabled');
          skipElapsedRefreshes(Date.now());
          scheduleRefreshTimer();
        }
      }
    }
  }

  // ---- cheapest-venue history: scheduled captures, not this page's ---------
  const history = config.history;
  const historyGrid = $('execution-history-grid');
  const historyEmpty = $('execution-history-empty');
  const historyMeta = $('execution-history-meta');
  const historyButtons = [...document.querySelectorAll('[data-history-days]')];
  let historyDays = Number(historyButtons[0]?.dataset.historyDays) || 7;
  let historyRequest = 0;
  function historyColumns(roundTrip) {
    return [
      ['Venue', '', ''],
      ['Cheapest', 'num', roundTrip
        ? 'Share of comparisons in which this venue had the lowest round-trip cost after fees; ties split.'
        : 'Share of comparisons in which this venue had the best fee-adjusted price; ties split.'],
      ['Gap', 'num', `Median extra cost versus the cheapest venue, in bps, when this venue was not cheapest${roundTrip ? ', for the whole round trip' : ''}.`, 'bps'],
      ['Quoted', 'num', `Share of captures with a complete, fresh quote at this size${roundTrip ? ' on both sides' : ''}.`],
    ];
  }
  function historyFees() {
    return Object.entries(feeChoices)
      .filter(([venue, tier]) => tier !== schedules[venue]?.default_tier)
      .map(([venue, tier]) => `${venue}:${tier}`).join(',');
  }
  function showHistoryEmpty(text) {
    historyGrid.replaceChildren();
    historyEmpty.textContent = text;
    historyEmpty.hidden = false;
  }
  function historyPeriod(data) {
    const first = Date.parse(data.first_capture);
    if (!Number.isFinite(first) || first <= Date.now() - (data.days - 0.1) * 86_400_000) return `last ${data.days}d`;
    return `since ${Fmt.dateTime(first, {seconds: false})}`;
  }
  // An absolute time stays true while the page sits open; amber once three
  // scheduled captures in a row are missing, so stalled collection is visible.
  function historyLatest(data) {
    const last = Date.parse(data.last_capture);
    if (!Number.isFinite(last)) return null;
    const age = (Date.now() - last) / 1000;
    const item = node('span', `latest ${age >= 86_400 ? Fmt.dateTime(data.last_capture) : Fmt.time(data.last_capture)}`);
    if (age > 3 * data.interval_seconds) {
      item.classList.add('text-phosphor');
      item.title = `No capture for ${Fmt.age(age)}; captures normally arrive every ${Math.round(data.interval_seconds / 60)} minutes.`;
    }
    return item;
  }
  function quotedTitle(venue) {
    const reasons = Object.entries(venue.excluded).map(([status, count]) => `${label(status)} ${Fmt.number(count, 0)}`);
    return `Complete, fresh quote in ${venue.quoted_pct ?? 0}% of ${Fmt.number(venue.samples, 0)} captures${reasons.length ? `; not comparable: ${reasons.join(', ')}` : ''}.`;
  }
  function historyCard(size, roundTrip) {
    const card = node('article', null, 'table-card execution-history-card');
    const header = node('div', null, 'panel-header');
    const title = node('h3', `${sizeLabel(size.notional_usd)} trade`, 'panel-title');
    title.append(node('span', `${Fmt.number(size.ranked, 0)} comparisons`, 'panel-subtitle'));
    header.append(title);
    const table = node('table', null, 'data-table execution-history-table');
    const headRow = node('tr');
    for (const [text, className, tip, unit] of historyColumns(roundTrip)) {
      const th = node('th', text, className);
      if (unit) th.append(', ', node('span', unit, 'unit'));
      th.scope = 'col';
      if (tip) th.title = tip;
      headRow.append(th);
    }
    const head = node('thead');
    head.append(headRow);
    const body = node('tbody');
    for (const venue of size.venues) {
      const row = node('tr');
      const name = node('th');
      name.scope = 'row';
      name.title = venue.label;
      const dot = node('span', null, 'status-dot');
      dot.style.color = colorFor(venue.identity);
      dot.setAttribute('aria-hidden', 'true');
      // The venue's display name; its product name is the tooltip.
      const heading = node('div', null, 'venue-heading');
      heading.append(dot, node('span', labelFor(venue.identity), 'venue-name'));
      name.append(heading);
      const share = node('td', null, 'num');
      if (venue.win_share_pct === null) {
        share.textContent = 'Not ranked';
        share.classList.add('text-faint');
        share.title = 'Fee unknown, so this venue is never ranked.';
      } else {
        share.classList.add('execution-history-share');
        share.style.setProperty('--share', `${venue.win_share_pct}%`);
        share.textContent = `${venue.win_share_pct.toFixed(1)}%`;
      }
      const gap = node('td', venue.median_gap_bps === null ? '—' : `+${venue.median_gap_bps.toFixed(1)}`, 'num');
      const quoted = node('td', venue.quoted_pct === null ? '—' : `${Math.round(venue.quoted_pct)}%`, 'num');
      quoted.title = quotedTitle(venue);
      row.append(name, share, gap, quoted);
      body.append(row);
    }
    table.append(head, body);
    card.append(header, table);
    return card;
  }
  function renderHistory(data) {
    historyMeta.replaceChildren(...[
      `${data.asset} ${data.side === 'round_trip' ? 'round trip' : data.side}`,
      historyPeriod(data),
      `every ${Math.round(data.interval_seconds / 60)} min`,
      historyLatest(data),
      data.custom_fees ? 'your selected fee tiers' : 'base taker fees',
      Number(hold.value) ? 'funding not included' : null,
      `${sizeLabel('10000000')} omitted: Hyperliquid book too shallow`,
    ].filter(Boolean).map(item => typeof item === 'string' ? node('span', item) : item));
    if (!data.sizes.some(size => size.ranked)) {
      showHistoryEmpty(`No comparisons yet for ${data.asset}. Venues are sampled every ${Math.round(data.interval_seconds / 60)} minutes; results appear after the first captures.`);
      return;
    }
    historyEmpty.hidden = true;
    historyGrid.replaceChildren(...data.sizes.map(size => historyCard(size, data.side === 'round_trip')));
  }
  async function loadHistory() {
    const request = ++historyRequest;
    const chosen = asset.value;
    if (!history.assets.includes(chosen)) {
      // This supersedes any in-flight load, whose finally no longer owns the busy flag.
      historyGrid.setAttribute('aria-busy', 'false');
      historyMeta.replaceChildren(node('span', `Collected for ${history.assets.join(', ') || 'no assets'}`));
      showHistoryEmpty(history.assets.length
        ? `History is collected for ${history.assets.join(', ')}. Choose one of those assets to see which venue is usually cheapest.`
        : 'No assets are configured for cheapest-venue history.');
      return;
    }
    const params = new URLSearchParams({asset: chosen, side: isRoundTrip(trade.value) ? 'round_trip' : trade.value, days: String(historyDays)});
    const fees = historyFees();
    if (fees) params.set('fees', fees);
    historyGrid.setAttribute('aria-busy', 'true');
    try {
      const data = await Overseer.requestJSON(`${history.endpoint}?${params}`);
      if (request === historyRequest) renderHistory(data);
    } catch (_) {
      if (request !== historyRequest) return;
      historyMeta.replaceChildren();
      showHistoryEmpty('Cheapest venue history could not be loaded. It reloads when you change the asset, trade, fees or window.');
    } finally {
      if (request === historyRequest) historyGrid.setAttribute('aria-busy', 'false');
    }
  }
  for (const button of historyButtons) {
    button.addEventListener('click', () => {
      historyDays = Number(button.dataset.historyDays);
      for (const other of historyButtons) other.setAttribute('aria-pressed', String(other === button));
      loadHistory();
    });
  }

  bindPriceTooltip();
  bindBestDetails();
  tbody.replaceChildren();
  for (const venueId of Object.keys(schedules)) buildRow(venueId);
  populateFeeSources();
  // Phones fold the toolbar behind one line, e.g. "BTC · buy · hold 24h".
  const syncFilters = Overseer.filtersFold(document.getElementById('execution-filters'), () => [
    asset.value,
    (trade.selectedOptions[0]?.textContent || trade.value).toLowerCase(),
    Number(hold.value) ? `hold ${holdLabels[hold.value] || `${hold.value}h`}` : null,
  ].filter(Boolean).join(' · '));
  for (const control of [asset, trade, hold]) {
    control.addEventListener('change', () => { syncFilters(); capture(); loadHistory(); });
  }
  sortButtons.forEach((button, index) => button.addEventListener('click', () => {
    sortIndex = index;
    sortFilters.set('sort', notionals[index]);
    sortRows();
  }));
  sortRows();
  // Dimmed (aria-disabled) while a capture runs; it keeps focus, unlike disabled.
  refresh.addEventListener('click', () => {
    if (refresh.getAttribute('aria-disabled') !== 'true') capture();
  });
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
    captureQueued = false;
    if (!controller) return;
    controller.abort();
    controller = null;
    table.setAttribute('aria-busy', 'false');
    refresh.removeAttribute('aria-disabled');
    showFailure('Comparison interrupted; automatic refresh resumes when this page is visible', 'Capture interrupted');
  });
  if (asset.options.length) {
    nextCaptureAt = Date.now();
    if (pageVisible()) capture();
    else scheduleRefreshTimer();
    loadHistory();
  }
  else {
    refresh.disabled = true;
    trade.disabled = true;
    hold.disabled = true;
    asset.disabled = true;
    setStatus('empty', 'No assets configured');
    for (const row of rows.values()) row.tr.hidden = true;
    pageState.empty('No crypto perpetual assets are configured for execution estimates. Add an asset to the symbol list to compare venues.');
    renderBestSummary();
    updateRefreshCadence();
  }
})();
