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
};

/* Fixed iteration order. Never sort by value, count, or API key order — that
 * would make a venue's colour depend on its rank. */
const VENUE_ORDER = Object.keys(VENUE_COLORS);

/* Shared chart chrome follows the application tokens. Keeping it here avoids
 * five chart configurations drifting back toward low-contrast legacy greys. */
const CHART_THEME = Object.freeze({
  text: '#ADB7C5',
  grid: '#263247',
  crosshair: '#8793A4',
  border: '#344156',
  zero: '#5A687F',
  accent: '#E6B85C',
  gain: '#64D69A',
  loss: '#FF7185',
  font: '"IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace',
});

/* Unknown venues (a symbol still in the DB for a venue dropped from config)
 * get a neutral grey rather than a generated hue. */
const colorFor = (v) => VENUE_COLORS[v] || CHART_THEME.text;
