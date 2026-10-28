/* Display formats shared by every page, so one value never prints two ways.
 *
 * - Signs use the true minus (U+2212); a value that rounds to zero is unsigned.
 * - Missing or non-numeric input prints as an em dash.
 * - Large amounts keep three significant figures with an upper-case suffix:
 *   35.7B, 7.01B, 184M, 410K.
 * - Basis points are always "bps".
 * - Times are always UTC: "15:19:00 UTC", "26 Oct 15:19:00 UTC".
 * - Ages: "12s", "3m 05s" under five minutes, then "14m", "2h", "3d".
 *
 * Loaded before reliability.js on every page (base.html) as the global Fmt.
 */
(() => {
  'use strict';

  const MINUS = '\u2212';
  const DASH = '\u2014';
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  const finite = value => value !== null && value !== undefined && value !== ''
    && typeof value !== 'boolean' && Number.isFinite(Number(value));

  const groupedCache = new Map();
  function grouped(digits) {
    if (!groupedCache.has(digits)) {
      groupedCache.set(digits, new Intl.NumberFormat('en-US', {
        minimumFractionDigits: digits, maximumFractionDigits: digits,
      }));
    }
    return groupedCache.get(digits);
  }

  // Sign for an already-formatted magnitude: nothing when it reads as zero.
  function withSign(negative, text, sign) {
    if (!/[1-9]/.test(text)) return text;
    if (negative) return MINUS + text;
    return sign ? `+${text}` : text;
  }

  // number(1234.5) → "1,234.50"; number(-0.004, 2) → "0.00".
  function number(value, digits = 2, { sign = false } = {}) {
    if (!finite(value)) return DASH;
    const n = Number(value);
    return withSign(n < 0, grouped(digits).format(Math.abs(n)), sign);
  }

  const signed = (value, digits = 2) => number(value, digits, { sign: true });

  // percent(12.345) → "12.35%"; signedPercent(-1.2) → "−1.20%".
  function percent(value, digits = 2, { sign = false } = {}) {
    return finite(value) ? `${number(value, digits, { sign })}%` : DASH;
  }
  const signedPercent = (value, digits = 2) => percent(value, digits, { sign: true });

  // bps(5) → "5.00 bps"; a non-zero cost too small to show reads "<0.01 bps".
  function bps(value, digits = 2, { sign = false } = {}) {
    if (!finite(value)) return DASH;
    const n = Number(value);
    const floor = 10 ** -digits;
    if (n !== 0 && Math.abs(n) < floor / 2) return `<${grouped(digits).format(floor)} bps`;
    return `${number(n, digits, { sign })} bps`;
  }

  // usd(1234.5) → "$1,234.50"; usd(-3, 0) → "−$3".
  function usd(value, digits = 2, { sign = false } = {}) {
    if (!finite(value)) return DASH;
    const n = Number(value);
    return withSign(n < 0, `$${grouped(digits).format(Math.abs(n))}`, sign);
  }

  // Three significant figures with K/M/B/T, e.g. 184_300_000 → "184M".
  const TIERS = [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K'], [1, '']];
  function compactMagnitude(abs) {
    if (abs === 0) return '0';
    for (let i = 0; i < TIERS.length; i += 1) {
      const [size, suffix] = TIERS[i];
      if (abs < size && i < TIERS.length - 1) continue;
      const scaled = abs / size;
      const digits = scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
      const text = grouped(digits).format(scaled);
      // 999,950 rounds to "1,000K": step up to "1.00M" instead.
      if (Number(text.replace(/,/g, '')) >= 1000 && i > 0) {
        return `${grouped(2).format(abs / TIERS[i - 1][0])}${TIERS[i - 1][1]}`;
      }
      return `${text}${suffix}`;
    }
    return grouped(2).format(abs);
  }
  function compact(value, { sign = false } = {}) {
    if (!finite(value)) return DASH;
    const n = Number(value);
    return withSign(n < 0, compactMagnitude(Math.abs(n)), sign);
  }
  function usdCompact(value, { sign = false } = {}) {
    if (!finite(value)) return DASH;
    const n = Number(value);
    return withSign(n < 0, `$${compactMagnitude(Math.abs(n))}`, sign);
  }

  // A published fee rate keeps its own precision: feeBps(3) → "3.0 bps",
  // feeBps(3.75) → "3.75 bps", feeBps(1.55) → "1.55 bps".
  function feeBps(value) {
    if (!finite(value)) return DASH;
    const n = Number(value);
    return bps(n, Math.abs(n * 10 - Math.round(n * 10)) < 1e-9 ? 1 : 2);
  }

  // leverage(9) → "9.0x"; a venue's cap, leverageCap(50) → "50x".
  const leverage = value => (finite(value) ? `${number(value, 1)}x` : DASH);
  const leverageCap = value => (finite(value) ? `${number(value, 0)}x` : DASH);

  // Accepts a Date, an ISO string, epoch milliseconds, or epoch seconds (as
  // chart libraries pass them: any number below 1e11).
  function toDate(value) {
    if (value === null || value === undefined || value === '') return null;
    let date;
    if (value instanceof Date) date = value;
    else if (typeof value === 'number') date = new Date(value < 1e11 ? value * 1000 : value);
    else date = new Date(value);
    return Number.isFinite(date.getTime()) ? date : null;
  }
  const pad = value => String(value).padStart(2, '0');

  // time(…) → "15:19:00 UTC"; with { seconds: false } → "15:19 UTC".
  function time(value, { seconds = true } = {}) {
    const date = toDate(value);
    if (!date) return DASH;
    const clock = `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
    return `${seconds ? `${clock}:${pad(date.getUTCSeconds())}` : clock} UTC`;
  }

  // dateTime(…) → "26 Oct 15:19:00 UTC"; with { seconds: false } → "26 Oct 15:19 UTC".
  function dateTime(value, { seconds = true } = {}) {
    const date = toDate(value);
    if (!date) return DASH;
    return `${date.getUTCDate()} ${MONTHS[date.getUTCMonth()]} ${time(date, { seconds })}`;
  }

  // age(65) → "1m 05s"; age(7200) → "2h". The health page's `age` filter
  // (views/pages.py) uses the same buckets.
  function age(totalSeconds) {
    if (!finite(totalSeconds)) return DASH;
    const seconds = Math.max(0, Math.floor(Number(totalSeconds)));
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 300) return `${Math.floor(seconds / 60)}m ${pad(seconds % 60)}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h`;
    return `${Math.floor(seconds / 86_400)}d`;
  }

  window.Fmt = Object.freeze({
    MINUS, DASH,
    number, signed, percent, signedPercent, bps, feeBps,
    usd, usdCompact, compact, leverage, leverageCap,
    time, dateTime, age,
  });
})();
