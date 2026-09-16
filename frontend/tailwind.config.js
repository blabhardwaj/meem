/** @type {import('tailwindcss').Config} */
// Color tokens live in src/index.css's @theme block (as CSS custom
// properties, so light-mode overrides and opacity variants both work
// automatically) — not here. See that file before adding a new color.
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  plugins: [],
}
