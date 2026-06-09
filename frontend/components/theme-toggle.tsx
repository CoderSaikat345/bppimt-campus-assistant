"use client";

/**
 * components/theme-toggle.tsx
 *
 * Minimalist theme toggle button. Uses `next-themes` `useTheme` hook to cycle
 * between light and dark modes. A `mounted` guard prevents hydration mismatch
 * by rendering a static placeholder until the client-side theme is resolved.
 *
 * The Sun/Moon icons cross-fade with a subtle scale transition for delight.
 */

import * as React from "react";
import { useTheme } from "next-themes";
import { Sun, Moon } from "lucide-react";

export function ThemeToggle(): React.ReactElement {
  const { resolvedTheme, setTheme } = useTheme();
  const [mounted, setMounted] = React.useState(false);

  // Prevent hydration mismatch — render a skeleton until mounted
  React.useEffect(() => setMounted(true), []);

  const toggleTheme = React.useCallback(() => {
    setTheme(resolvedTheme === "dark" ? "light" : "dark");
  }, [resolvedTheme, setTheme]);

  // Skeleton placeholder during SSR / hydration
  if (!mounted) {
    return (
      <div
        className="h-8 w-8 rounded-xl bg-surface-100 dark:bg-surface-800 animate-pulse"
        aria-hidden
      />
    );
  }

  const isDark = resolvedTheme === "dark";

  return (
    <button
      type="button"
      onClick={toggleTheme}
      aria-label={isDark ? "Switch to light mode" : "Switch to dark mode"}
      className={[
        "relative flex h-8 w-8 items-center justify-center rounded-xl",
        "text-surface-500 dark:text-surface-400",
        "hover:text-surface-900 dark:hover:text-surface-100",
        "hover:bg-surface-100 dark:hover:bg-surface-800",
        "transition-all duration-200",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500",
      ].join(" ")}
    >
      {/* Sun icon — visible in dark mode (click to switch to light) */}
      <Sun
        className={[
          "absolute h-4 w-4 transition-all duration-300",
          isDark
            ? "rotate-0 scale-100 opacity-100"
            : "rotate-90 scale-0 opacity-0",
        ].join(" ")}
        aria-hidden
      />

      {/* Moon icon — visible in light mode (click to switch to dark) */}
      <Moon
        className={[
          "absolute h-4 w-4 transition-all duration-300",
          isDark
            ? "-rotate-90 scale-0 opacity-0"
            : "rotate-0 scale-100 opacity-100",
        ].join(" ")}
        aria-hidden
      />
    </button>
  );
}
