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

  const PAGE_STATE_CLASS = {
    loading: 'mb-4 flex min-h-11 items-center gap-3 rounded border border-line bg-panel/60 px-3 py-2 font-mono text-data text-dim',
    empty: 'mb-4 flex min-h-11 items-center gap-3 rounded border border-line bg-panel/60 px-3 py-2 font-mono text-data text-dim',
    error: 'mb-4 flex min-h-11 items-center gap-3 rounded border border-loss/40 bg-loss/10 px-3 py-2 font-mono text-data text-loss',
  };

  function pageState(root = document.getElementById('page-status')) {
    const successes = new Map();
    const failures = new Map();
    const empties = new Map();
    const message = root?.querySelector('[data-status-message]');
    const retry = root?.querySelector('[data-status-retry]');

    function hide() {
      if (!root) return;
      root.classList.add('hidden');
      retry.onclick = null;
    }

    function show(kind, text, retryAction = null) {
      if (!root || !message || !retry) return;
      root.className = PAGE_STATE_CLASS[kind];
      root.setAttribute('role', kind === 'error' ? 'alert' : 'status');
      message.textContent = text;
      retry.classList.toggle('hidden', !retryAction);
      retry.onclick = retryAction ? () => {
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
      if (empty) show('empty', empty.text, empty.retryAction);
      else hide();
    }

    return {
      get hasLoaded() { return successes.has('page'); },
      loading(text = 'Loading latest data…') {
        if (!successes.has('page') && !failures.size) show('loading', text);
      },
      ready(key = 'page') {
        successes.set(key, new Date());
        failures.delete(key);
        empties.delete(key);
        renderState();
      },
      empty(text, retryAction = null, key = 'page') {
        successes.set(key, new Date());
        failures.delete(key);
        empties.set(key, { text, retryAction });
        renderState();
      },
      error(resource, retryAction, key = 'page') {
        const lastSuccess = successes.get(key);
        const prior = lastSuccess
          ? ` Showing the last update from ${lastSuccess.toISOString().slice(11, 19)} UTC.`
          : '';
        failures.set(key, {
          text: `${resource} could not be loaded.${prior}`,
          retryAction,
        });
        renderState();
      },
    };
  }

  function formatAge(totalSeconds) {
    const seconds = Math.max(0, Math.floor(totalSeconds));
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 300) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h`;
    return `${Math.floor(seconds / 86_400)}d`;
  }

  function formatUtcTimestamp(value, includeDate = false) {
    const date = value instanceof Date ? value : new Date(value);
    if (!Number.isFinite(date.getTime())) return null;
    const iso = date.toISOString();
    return includeDate
      ? `${iso.slice(0, 10)} ${iso.slice(11, 19)} UTC`
      : `${iso.slice(11, 19)} UTC`;
  }
  const screenPollers = new Set();

  function formatCheckDelay(milliseconds) {
    const seconds = Math.max(0, Math.ceil(milliseconds / 1000));
    if (seconds < 60) return `${seconds}s`;
    return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, '0')}s`;
  }

  function renderScreenCheck() {
    const indicators = [...document.querySelectorAll('[data-freshness]')];
    if (!indicators.length) return;
    const pollers = [...screenPollers];
    const checking = pollers.some(poller => poller.checking);
    const nextAt = pollers.reduce(
      (soonest, poller) => Math.min(soonest, poller.nextAt ?? Infinity),
      Infinity,
    );

    let label;
    let checkTitle;
    if (checking) {
      label = 'refreshing data';
      checkTitle = 'Forced data refresh in progress';
    } else if (Number.isFinite(nextAt)) {
      const delay = formatCheckDelay(nextAt - Date.now());
      label = null;
      checkTitle = `Next forced data refresh in ${delay}`;
    } else {
      label = null;
      checkTitle = 'This screen refreshes on load or when its controls change';
    }

    for (const indicator of indicators) {
      const healthTitle = indicator.dataset.healthTitle || 'Data freshness is being checked';
      const visibleLabel = label
        || (indicator.dataset.dataTimestamp
          ? `data · ${indicator.dataset.dataTimestamp}`
          : 'data timestamp —');
      indicator.querySelector('[data-freshness-label]').textContent = visibleLabel;
      indicator.title = `${checkTitle}. ${healthTitle}.`;
      indicator.setAttribute('aria-label', `${checkTitle}. ${healthTitle}.`);
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

  const FRESHNESS_TONE = {
    checking: ['bg-faint', 'text-dim', 'border-line'],
    fresh: ['bg-gain', 'text-gain', 'border-gain/40'],
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

    function setBanner(kind, text) {
      if (!banner) return;
      if (!text) {
        banner.classList.add('hidden');
        return;
      }
      const loss = kind === 'error' || kind === 'stale';
      banner.className = `border-b px-4 py-1.5 text-center font-mono text-label sm:px-6 ${
        loss
          ? 'border-loss/40 bg-loss/10 text-loss'
          : 'border-phosphor/40 bg-phosphor/10 text-phosphor'
      }`;
      banner.setAttribute('role', loss ? 'alert' : 'status');
      banner.textContent = text;
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
        const timestamp = formatUtcTimestamp(sample.lastTs, true) || 'timestamp unavailable';
        const description = checkFailed ? 'Freshness check failed; last confirmed'
          : state === 'fresh' ? 'Fresh data; newest'
          : state === 'delayed' ? 'Data delayed; newest'
          : 'Stale data; newest';
        title = `${description} bar: ${timestamp}`;
        if (checkFailed) {
          setBanner('error', `Freshness check failed — newest confirmed bar: ${timestamp}.`);
        } else if (age > 300) {
          setBanner('stale', `Data stale — newest bar: ${timestamp} (${formatAge(age)} old).`);
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
      for (const indicator of indicators) {
        const dot = indicator.querySelector('[data-freshness-dot]');
        const text = indicator.querySelector('[data-freshness-label]');
        const tone = FRESHNESS_TONE[state];
        indicator.classList.remove(...ALL_BORDER_TONES);
        indicator.classList.add(tone[2]);
        indicator.dataset.state = state;
        indicator.dataset.healthTitle = title;
        const dataTimestamp = sample && formatUtcTimestamp(sample.lastTs);
        if (dataTimestamp) indicator.dataset.dataTimestamp = dataTimestamp;
        else delete indicator.dataset.dataTimestamp;
        dot.classList.remove(...ALL_DOT_TONES);
        dot.classList.add(tone[0]);
        text.classList.remove(...ALL_TEXT_TONES);
        text.classList.add(tone[1]);
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
    formatAge,
    formatUtcTimestamp,
    poll,
    filterState,
    startFreshness,
  });
})();
