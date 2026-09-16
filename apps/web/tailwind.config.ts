import type { Config } from "tailwindcss";

// Architecture §5.3 / ADR 0012: every color is a CSS variable fed by the tenant theme.
// No raw hex values in components — only these semantic names.
const token = (name: string) => `rgb(var(--color-${name}) / <alpha-value>)`;

export default {
  content: ["./src/**/*.{ts,tsx}"],
  darkMode: ["selector", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        primary: token("primary"),
        "primary-fg": token("primary-fg"),
        secondary: token("secondary"),
        accent: token("accent"),
        bg: token("bg"),
        surface: token("surface"),
        fg: token("fg"),
        muted: token("muted"),
        border: token("border"),
        danger: token("danger"),
        success: token("success"),
      },
      borderRadius: { theme: "var(--radius)" },
      fontFamily: { body: "var(--font-body)", heading: "var(--font-heading)" },
    },
  },
  plugins: [],
} satisfies Config;
