import type { Config } from "tailwindcss";

/**
 * Tokens extracted verbatim from docs/design/purse-design-direction.html.
 * Every color is wired to a CSS variable (see app/globals.css) so the dark
 * default and the light theme share one token surface. Brass (#C9A24B) is the
 * SOLE accent; ok/danger are signals, never the accent.
 */
const config: Config = {
  darkMode: ["class", '[data-theme="dark"]'],
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        surface: "var(--surface)",
        "surface-2": "var(--surface-2)",
        raised: "var(--raised)",
        border: "var(--border)",
        "border-soft": "var(--border-soft)",
        text: "var(--text)",
        muted: "var(--muted)",
        faint: "var(--faint)",
        accent: "var(--accent)",
        "accent-soft": "var(--accent-soft)",
        "accent-line": "var(--accent-line)",
        ok: "var(--ok)",
        "ok-soft": "var(--ok-soft)",
        danger: "var(--danger)",
        "danger-soft": "var(--danger-soft)",
      },
      fontFamily: {
        sans: "var(--sans)",
        mono: "var(--mono)",
      },
      borderRadius: {
        DEFAULT: "var(--radius)",
      },
      maxWidth: {
        wrap: "var(--maxw)",
      },
    },
  },
  plugins: [],
};

export default config;
