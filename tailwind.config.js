/** overseer design tokens — terminal-heritage console theme.
 * Rebuild after editing templates:  npm run css
 */
module.exports = {
  content: [
    "./src/web/templates/**/*.html",
    "./src/web/static/js/**/*.js",
  ],
  theme: {
    // A compact desktop scale, not a tiny one. Labels stay dense, but every
    // normal reading size now clears the 14px floor. Mono is reserved for data
    // in the templates; page chrome inherits the sans stack below.
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
        // faint >=5.1:1. Faint is now genuinely secondary, not illegible.
        ink:          "#080B12",
        panel:        "#111721",
        panel2:       "#192230",
        line:         "#344156",
        "line-strong":"#5A687F",
        phosphor:     "#E6B85C",
        gain:         "#64D69A",
        loss:         "#FF7185",
        body:         "#EEF2F7",
        dim:          "#ADB7C5",
        faint:        "#8793A4",
      },
      // Terminal character comes from data, timestamps and compact controls;
      // prose and navigation use the platform sans stack for faster scanning.
      fontFamily: {
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo',
               'Consolas', '"Liberation Mono"', 'monospace'],
        sans: ['Inter', 'ui-sans-serif', 'system-ui', '-apple-system',
               'BlinkMacSystemFont', '"Segoe UI"', 'sans-serif'],
      },
      borderRadius: { DEFAULT: "4px", md: "8px" },
      letterSpacing: { label: "0.1em" },
    },
  },
  plugins: [],
};
