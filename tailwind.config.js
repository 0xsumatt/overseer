/** overseer design tokens — terminal-heritage console theme.
 * Source CSS lives in src/web/static/css (assets.css imports the partials).
 * Rebuild after editing templates, scripts or styles:  npm run css
 */
module.exports = {
  content: [
    "./src/web/templates/**/*.html",
    "./src/web/static/js/**/*.js",
  ],
  theme: {
    // A compact desktop scale, not a tiny one. Labels stay dense at 12px;
    // every reading size clears the 14px floor.
    fontSize: {
      label: ['12px', '1.45'],
      data:  ['14px', '1.5'],
      title: ['24px', '1.2'],
      hero:  ['28px', '1.1'],
    },
    extend: {
      colors: {
        // Instrument-inspired blue-black surfaces. The three text roles all
        // clear WCAG AA on every surface: body >=14:1, dim >=7.8:1,
        // faint >=5.1:1. Faint is genuinely secondary, not illegible.
        ink:          "#080B12",
        panel:        "#111721",
        panel2:       "#192230",
        "line-soft":  "#263247",
        line:         "#344156",
        "line-strong":"#5A687F",
        phosphor:     "#E6B85C",
        gain:         "#64D69A",
        loss:         "#FF7185",
        // Position direction, deliberately distinct from gain/loss:
        // a short position is not a loss.
        long:         "#85B8F4",
        short:        "#C1A2F2",
        body:         "#EEF2F7",
        dim:          "#ADB7C5",
        faint:        "#8793A4",
      },
      // Mono carries the terminal voice: data, labels, controls, navigation
      // and system status messages. Sans is reserved for page titles and prose.
      // Both families are self-hosted (css/fonts.css).
      fontFamily: {
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo',
               'Consolas', '"Liberation Mono"', 'monospace'],
        sans: ['"IBM Plex Sans"', 'ui-sans-serif', 'system-ui', '-apple-system',
               'BlinkMacSystemFont', '"Segoe UI"', 'sans-serif'],
      },
      borderRadius: { DEFAULT: "4px", md: "8px" },
      letterSpacing: { label: "0.1em" },
      boxShadow: {
        popover: "0 12px 32px -8px rgb(0 0 0 / 0.6)",
      },
    },
  },
  plugins: [],
};
