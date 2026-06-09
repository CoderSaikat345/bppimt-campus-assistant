import type { Config } from "tailwindcss";
import animate from "tailwindcss-animate";

const config: Config = {
  darkMode: ["class"],
  content: [
    "./pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ["var(--font-inter)", "system-ui", "sans-serif"],
        mono: ["var(--font-jetbrains-mono)", "monospace"],
      },
      colors: {
        // BPPIMT brand palette
        brand: {
          50:  "#eef4ff",
          100: "#dae6ff",
          200: "#bdd2ff",
          300: "#90b4fe",
          400: "#5d8cf9",
          500: "#3b67f3",
          600: "#2347e8",
          700: "#1b35d4",
          800: "#1d2dab",
          900: "#1e2c87",
          950: "#161d53",
        },
        // Neutral surface palette
        surface: {
          0:   "#ffffff",
          50:  "#f8f9fc",
          100: "#f1f3f9",
          200: "#e4e8f2",
          300: "#c8d0e4",
          400: "#9aaac6",
          500: "#6b7fa3",
          600: "#4f6080",
          700: "#3c4a62",
          800: "#28334a",
          900: "#161e30",
          950: "#0d1320",
        },
      },
      borderRadius: {
        xl: "0.75rem",
        "2xl": "1rem",
        "3xl": "1.5rem",
      },
      keyframes: {
        "fade-in": {
          from: { opacity: "0", transform: "translateY(6px)" },
          to:   { opacity: "1", transform: "translateY(0)" },
        },
        "slide-up": {
          from: { opacity: "0", transform: "translateY(16px)" },
          to:   { opacity: "1", transform: "translateY(0)" },
        },
        "pulse-dot": {
          "0%, 80%, 100%": { transform: "scale(0.6)", opacity: "0.4" },
          "40%":           { transform: "scale(1)",   opacity: "1"   },
        },
        shimmer: {
          from: { backgroundPosition: "-200% 0" },
          to:   { backgroundPosition: "200% 0"  },
        },
      },
      animation: {
        "fade-in":  "fade-in 0.25s ease-out both",
        "slide-up": "slide-up 0.3s cubic-bezier(0.16, 1, 0.3, 1) both",
        "pulse-dot": "pulse-dot 1.4s ease-in-out infinite",
        shimmer:    "shimmer 2s linear infinite",
      },
      boxShadow: {
        "card":  "0 1px 3px 0 rgba(0,0,0,.08), 0 1px 2px -1px rgba(0,0,0,.06)",
        "card-md": "0 4px 16px -2px rgba(0,0,0,.12), 0 2px 6px -2px rgba(0,0,0,.08)",
        "glow-brand": "0 0 24px -4px rgba(59,103,243,.45)",
      },
    },
  },
  plugins: [animate],
};

export default config;
