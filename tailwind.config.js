/** overseer design tokens — terminal-heritage console theme.
 * Rebuild after editing templates:  npm run css
 */
module.exports = {
  content: [
    "./src/web/templates/**/*.html",
    "./src/web/static/js/**/*.js",
  ],
  theme: {
    extend: {
      colors: {
        ink:      "#0B0E16",   // page background — blue-black, not pure black
        panel:    "#111624",   // raised surfaces
        panel2:   "#161D2E",   // hover / inset surfaces
        line:     "#232B3D",   // hairline borders
        phosphor: "#FFB454",   // brand amber — terminal phosphor; used sparingly
        gain:     "#4ADE80",
        loss:     "#FB7185",
        body:     "#C8D0E0",   // primary text
        dim:      "#667089",   // secondary text
        faint:    "#3D465C",   // tertiary / disabled
      },
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas',
               '"Liberation Mono"', 'monospace'],
        sans: ['ui-sans-serif', 'system-ui', '-apple-system', '"Segoe UI"',
               'Roboto', 'sans-serif'],
      },
      borderRadius: { DEFAULT: "46x", md: "8px" },
      letterSpacing: { label: "0.14em" },
    },
  },
  plugins: [],
};
