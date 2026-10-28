/* Shared read-path reliability primitives for every dashboard page. */
(() => {
  'use strict';

  class RequestError extends Error {
    constructor(message, status = null) {
      super(message);
      this.name = 'RequestError';
      this.status = status;
    }
  }

  async function requestJSON(url, { timeoutMs = 12_000, forceRefresh = false } = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    const headers = { Accept: 'application/json' };
    if (forceRefresh) headers['Cache-Control'] = 'no-cache';
    try {
      const response = await fetch(url, {
        cache: forceRefresh ? 'reload' : 'default',
        headers,
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new RequestError('The server could not complete this request.', response.status);
      }
      const contentType = response.headers.get('content-type') || '';
      if (!contentType.includes('application/json')) {
        throw new RequestError('The server returned an unexpected response.', response.status);
      }
      return await response.json();
    } catch (error) {
      if (error.name === 'AbortError') {
        throw new RequestError('The request timed out.');
      }
      if (error instanceof RequestError) throw error;
      throw new RequestError(navigator.onLine
        ? 'The server could not be reached.'
        : 'This device is offline.');
    } finally {
      clearTimeout(timeout);
    }
  }

  // One slot per page (rendered by the page_status() macro under the header).
  // Loading and empty are neutral; only failures are tinted, and only
  // failures offer a retry: retrying cannot conjure data that does not exist.
  const PAGE_STATE_TONE = { loading: 'info', empty: 'info', error: 'error' };

  function pageState(root = document.getElementById('page-status')) {
    const successes = new Map();
    const failures = new Map();
    const empties = new Map();
    const message = root?.querySelector('[data-status-message]');
    const retry = root?.querySelector('[data-status-retry]');
    const cursor = document.createElement('span');
    cursor.className = 'cursor';
    cursor.setAttribute('aria-hidden', 'true');

    function hide() {
      if (!root) return;
      root.classList.add('hidden');
      retry.onclick = null;
    }

    function show(kind, text, retryAction = null) {
      if (!root || !message || !retry) return;
      root.className = 'notice mb-4';
      root.dataset.tone = PAGE_STATE_TONE[kind];
      root.dataset.state = kind;
      root.setAttribute('role', kind === 'error' ? 'alert' : 'status');
      message.textContent = text;
      if (kind === 'loading') message.append(cursor);
      const canRetry = kind === 'error' && Boolean(retryAction);
      retry.classList.toggle('hidden', !canRetry);
      retry.onclick = canRetry ? () => {
        retry.disabled = true;
        retryAction();
      } : null;
      retry.disabled = false;
    }

    function renderState() {
      const failure = [...failures.values()].at(-1);
      if (failure) {
        show('error', failure.text, failure.retryAction);
        return;
      }
      const empty = [...empties.values()].at(-1);
      if (empty) show('empty', empty.text);
      else hide();
    }

    return {
      get hasLoaded() { return successes.has('page'); },
      loading(text = 'Loading latest data') {
        if (!successes.has('page') && !failures.size) show('loading', text);
      },
      ready(key = 'page') {
        successes.set(key, new Date());
        failures.delete(key);
        empties.delete(key);
        renderState();
      },
      // `text` should say what is missing and what to change, e.g.
      // "No trades for AAPL in 6h. Choose another asset or a longer window."
      empty(text, key = 'page') {
        successes.set(key, new Date());
        failures.delete(key);
        empties.set(key, { text });
        renderState();
      },
      error(resource, retryAction, key = 'page') {
        const lastSuccess = successes.get(key);
        const prior = lastSuccess
          ? ` Showing the last update from ${Fmt.time(lastSuccess)}.`
          : '';
        failures.set(key, {
          text: `${resource} could not be loaded.${prior}`,
          retryAction,
        });
        renderState();
      },
    };
  }

  const screenPollers = new Set();

  // A countdown reads like an age: "48s", "4m 05s".
  const formatCheckDelay = milliseconds => Fmt.age(Math.ceil(Math.max(0, milliseconds) / 1000));

  function renderScreenCheck() {
    const indicators = [...document.querySelectorAll('[data-freshness]')];
    if (!indicators.length) return;
    const pollers = [...screenPollers];
    const checking = pollers.some(poller => poller.checking);
    const nextAt = pollers.reduce(
      (soonest, poller) => Math.min(soonest, poller.nextAt ?? Infinity),
      Infinity,
    );

    let checkLabel;
    let checkTitle;
    if (checking) {
      checkLabel = 'Screen refreshing';
      checkTitle = 'Forced screen refresh in progress';
    } else if (Number.isFinite(nextAt)) {
      const delay = formatCheckDelay(nextAt - Date.now());
      checkLabel = `Screen refresh in ${delay}`;
      checkTitle = `Next forced screen refresh in ${delay}`;
    } else {
      checkLabel = 'Screen refresh on demand';
      checkTitle = 'This screen refreshes on load or when its controls change';
    }

    for (const indicator of indicators) {
      const healthTitle = indicator.dataset.healthTitle || 'Data freshness is being checked';
      indicator.querySelector('[data-freshness-check]').textContent = checkLabel;
      const ageLabel = indicator.querySelector('[data-freshness-label]').textContent;
      const stateLabel = indicator.querySelector('[data-freshness-state]').textContent;
      const description = `Market feed: ${ageLabel}. ${stateLabel}. ${healthTitle}. ${checkTitle}. `
        + 'This measures the newest bar across all markets, not every displayed series.';
      indicator.title = description;
      indicator.setAttribute('aria-label', description);
    }
  }

  function poll(callback, seconds) {
    if (typeof callback !== 'function' || !Number.isFinite(seconds) || seconds <= 0) {
      throw new TypeError('poll requires a callback and a positive interval in seconds');
    }
    const poller = {
      checking: false,
      nextAt: null,
      timer: null,
      cancelled: false,
      due: false,
      resume: null,
    };
    screenPollers.add(poller);

    async function run() {
      clearTimeout(poller.timer);
      poller.timer = null;
      if (poller.cancelled) return;
      if (document.hidden) {
        poller.checking = false;
        poller.nextAt = null;
        poller.due = true;
        renderScreenCheck();
        return;
      }

      poller.checking = true;
      poller.nextAt = null;
      poller.due = false;
      renderScreenCheck();
      try {
        await callback({ forceRefresh: true });
      } catch (error) {
        console.error('Scheduled screen check failed', error);
      } finally {
        if (poller.cancelled) return;
        poller.checking = false;
        poller.nextAt = Date.now() + seconds * 1000;
        renderScreenCheck();
        poller.timer = setTimeout(() => void run(), seconds * 1000);
      }
    }

    poller.resume = () => {
      if (document.hidden || poller.cancelled || poller.checking) return;
      if (!poller.due && (poller.nextAt === null || poller.nextAt > Date.now())) return;
      void run();
    };

    void run();
    return () => {
      poller.cancelled = true;
      clearTimeout(poller.timer);
      screenPollers.delete(poller);
      renderScreenCheck();
    };
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    for (const poller of screenPollers) poller.resume();
  });

  function filterState(page) {
    if (typeof page !== 'string' || !page) {
      throw new TypeError('filterState requires a page name');
    }
    const storageKey = `overseer.filters.${page}`;
    const values = Object.create(null);
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
      if (saved && typeof saved === 'object' && !Array.isArray(saved)) {
        for (const [name, value] of Object.entries(saved)) {
          if (typeof value === 'string') values[name] = value;
        }
      }
    } catch (error) {
      console.warn(`Could not restore ${page} filters`, error);
    }

    function get(name) {
      return typeof values[name] === 'string' ? values[name] : null;
    }

    function set(name, value) {
      if (value == null) delete values[name];
      else values[name] = String(value);
      try {
        localStorage.setItem(storageKey, JSON.stringify(values));
      } catch (error) {
        console.warn(`Could not save ${page} filters`, error);
      }
    }

    function getList(name) {
      try {
        const list = JSON.parse(get(name) || '[]');
        return Array.isArray(list) ? list.filter(value => typeof value === 'string') : [];
      } catch (error) {
        return [];
      }
    }

    function setList(name, valuesToSave) {
      set(name, JSON.stringify([...valuesToSave]));
    }

    function restoreSelect(select, name = select.id) {
      const saved = get(name);
      if (saved === null || ![...select.options].some(option => option.value === saved)) {
        return false;
      }
      select.value = saved;
      return true;
    }

    return Object.freeze({ get, set, getList, setList, restoreSelect });
  }

  // Marks a scroll box (.scroll-fade) whose content continues past its
  // bottom edge, so CSS fades the last visible line instead of cutting a row
  // in half with no sign there is more. Follows scrolling, resizing and
  // re-rendered content.
  function scrollFade(box) {
    if (!box) return () => {};
    const update = () => {
      const more = box.scrollTop + box.clientHeight < box.scrollHeight - 2;
      box.dataset.more = String(more);
    };
    box.addEventListener('scroll', update, { passive: true });
    new ResizeObserver(update).observe(box);
    new MutationObserver(update).observe(box, { childList: true, subtree: true });
    update();
    return update;
  }

  // Drives a ui.filters() fold. On phones it is a disclosure (closed at
  // first, remembering what you chose); from 640px it is always open and CSS
  // dissolves it, so the filters sit on the page. describe() returns the
  // summary line ("BTC · buy · 24h"); call the returned function whenever a
  // filter changes.
  function filtersFold(details, describe) {
    if (!details) return () => {};
    const summary = details.querySelector(':scope > summary');
    const body = details.querySelector(':scope > .filters-body');
    const selection = details.querySelector('[data-filters-selection]');
    const desktop = window.matchMedia('(min-width: 640px)');
    let openOnPhone = false;
    function sync() {
      if (desktop.matches) {
        details.open = true;
        // The summary vanishes on desktop; keep keyboard focus somewhere real.
        if (document.activeElement === summary) {
          body.querySelector('input, select, button, [tabindex]')?.focus();
        }
      } else {
        // Never hide a control someone is using when the viewport narrows.
        if (body.contains(document.activeElement)) openOnPhone = true;
        details.open = openOnPhone;
      }
    }
    details.addEventListener('toggle', () => {
      if (!desktop.matches) openOnPhone = details.open;
    });
    desktop.addEventListener('change', sync);
    const update = () => {
      if (selection) selection.textContent = describe();
    };
    sync();
    update();
    return update;
  }

  // [dot, text, status-bar border]. A healthy feed keeps the neutral edge
  // every page's status bar has; only a problem colours the frame.
  const FRESHNESS_TONE = {
    checking: ['bg-faint', 'text-dim', 'border-line-soft'],
    fresh: ['bg-gain', 'text-gain', 'border-line-soft'],
    delayed: ['bg-phosphor', 'text-phosphor', 'border-phosphor/40'],
    stale: ['bg-loss', 'text-loss', 'border-loss/40'],
    error: ['bg-loss', 'text-loss', 'border-loss/40'],
  };
  const ALL_DOT_TONES = Object.values(FRESHNESS_TONE).map(v => v[0]);
  const ALL_TEXT_TONES = Object.values(FRESHNESS_TONE).map(v => v[1]);
  const ALL_BORDER_TONES = Object.values(FRESHNESS_TONE).map(v => v[2]);

  function startFreshness() {
    const indicators = [...document.querySelectorAll('[data-freshness]')];
    const banner = document.getElementById('stale-banner');
    if (!indicators.length) return;

    let sample = null;
    let sampleClock = 0;
    let checkFailed = false;
    let emptyData = false;
    let freshnessTimer = null;
    let nextFreshnessAt = null;
    let freshnessDue = false;
    let refreshing = false;

    function ageNow() {
      return sample == null ? null : sample.age + (performance.now() - sampleClock) / 1000;
    }

    // A problem tints the status bar itself, so the frame never grows a
    // second strip; #stale-banner is a visually hidden live region that
    // announces the same message to screen readers.
    function setBanner(kind, text) {
      const alert = !text ? null : kind === 'error' || kind === 'stale' ? 'error' : 'warning';
      for (const indicator of indicators) {
        if (alert) indicator.dataset.alert = alert;
        else delete indicator.dataset.alert;
      }
      if (!banner) return;
      banner.setAttribute('role', alert === 'error' ? 'alert' : 'status');
      const message = text || '';
      if (banner.textContent !== message) banner.textContent = message;
    }

    function render() {
      const age = ageNow();
      let state = 'checking';
      let title = 'Data freshness is being checked';

      if (!navigator.onLine) {
        state = 'error';
        title = 'This device is offline';
        setBanner('error', 'Freshness unavailable — this device is offline.');
      } else if (sample) {
        state = checkFailed ? 'error' : age <= 120 ? 'fresh' : age <= 300 ? 'delayed' : 'stale';
        const timestamp = Fmt.dateTime(sample.lastTs);
        const description = checkFailed ? 'Freshness check failed; last confirmed'
          : state === 'fresh' ? 'Fresh data; newest'
          : state === 'delayed' ? 'Data delayed; newest'
          : 'Stale data; newest';
        title = `${description} bar: ${timestamp}`;
        if (checkFailed) {
          setBanner('error', `Freshness check failed — newest confirmed bar: ${timestamp}.`);
        } else if (age > 300) {
          setBanner('stale', `Data stale — newest bar: ${timestamp} (${Fmt.age(age)} old).`);
        } else {
          setBanner(null, null);
        }
      } else if (checkFailed) {
        state = 'error';
        title = 'Freshness check failed';
        setBanner('error', 'Freshness unavailable — the newest bar could not be checked.');
      } else if (emptyData) {
        state = 'delayed';
        title = 'No market-data bars have been ingested';
        setBanner('delayed', 'No data yet — ingest has not written a bar.');
      }
      const ageLabel = age === null
        ? emptyData && state !== 'error' ? 'No data yet' : 'Age unknown'
        : `${Fmt.age(age)} old`;
      const stateLabel = state === 'error'
        ? navigator.onLine ? 'Check failed' : 'Offline'
        : emptyData ? 'No bars'
        : { checking: 'Checking', fresh: 'Fresh', delayed: 'Delayed', stale: 'Stale' }[state];
      const stamp = sample && (age >= 86_400 ? Fmt.dateTime(sample.lastTs) : Fmt.time(sample.lastTs));
      const timestamp = stamp && stamp !== Fmt.DASH ? stamp : null;
      const lastConfirmed = checkFailed || !navigator.onLine;
      for (const indicator of indicators) {
        const dot = indicator.querySelector('[data-freshness-dot]');
        const status = indicator.querySelector('[data-freshness-status]');
        const time = indicator.querySelector('[data-freshness-timestamp]');
        const tone = FRESHNESS_TONE[state];
        indicator.classList.remove(...ALL_BORDER_TONES);
        indicator.classList.add(tone[2]);
        indicator.dataset.state = state;
        indicator.dataset.healthTitle = title;
        indicator.querySelector('[data-freshness-label]').textContent = ageLabel;
        indicator.querySelector('[data-freshness-state]').textContent = stateLabel;
        time.textContent = timestamp
          ? `${lastConfirmed ? 'Last confirmed bar' : 'Latest bar'} · ${timestamp}`
          : 'Latest bar · time unknown';
        if (timestamp) time.dateTime = sample.lastTs;
        else time.removeAttribute('datetime');
        dot.classList.remove(...ALL_DOT_TONES);
        dot.classList.add(tone[0]);
        status.classList.remove(...ALL_TEXT_TONES);
        status.classList.add(tone[1]);
      }
      renderScreenCheck();
    }

    async function refresh() {
      if (!navigator.onLine) {
        checkFailed = true;
        render();
        return;
      }
      try {
        const result = await requestJSON(
          '/api/freshness',
          { timeoutMs: 8_000, forceRefresh: true },
        );
        checkFailed = false;
        if (result.age_seconds == null) {
          sample = null;
          emptyData = true;
          sampleClock = performance.now();
          render();
          return;
        }
        emptyData = false;
        sample = { age: result.age_seconds, lastTs: result.last_ts };
        sampleClock = performance.now();
        render();
      } catch (error) {
        checkFailed = true;
        render();
      }
    }

    async function runRefresh() {
      clearTimeout(freshnessTimer);
      freshnessTimer = null;
      if (refreshing) return;
      if (document.hidden) {
        freshnessDue = true;
        nextFreshnessAt = null;
        return;
      }

      refreshing = true;
      freshnessDue = false;
      nextFreshnessAt = null;
      try {
        await refresh();
      } finally {
        refreshing = false;
        nextFreshnessAt = Date.now() + 60_000;
        freshnessTimer = setTimeout(() => void runRefresh(), 60_000);
      }
    }

    function resumeFreshness() {
      if (document.hidden || refreshing) return;
      if (!freshnessDue && (nextFreshnessAt === null || nextFreshnessAt > Date.now())) return;
      void runRefresh();
    }

    render();
    void runRefresh();
    setInterval(render, 1_000);
    document.addEventListener('visibilitychange', resumeFreshness);
    window.addEventListener('online', () => {
      freshnessDue = true;
      resumeFreshness();
    });
    window.addEventListener('offline', render);
  }

  window.Overseer = Object.freeze({
    requestJSON,
    pageState,
    poll,
    filterState,
    scrollFade,
    filtersFold,
    startFreshness,
  });
})();
