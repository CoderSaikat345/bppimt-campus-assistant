"use client";

/**
 * components/theme-provider.tsx
 *
 * Wraps the application with `next-themes` ThemeProvider for dark/light mode
 * switching. Uses `attribute="class"` to work with Tailwind CSS's `darkMode: "class"`
 * strategy. `suppressHydrationWarning` prevents React mismatch warnings since
 * the theme is read from localStorage on the client before hydration.
 */

import { ThemeProvider as NextThemesProvider } from "next-themes";
import type { ReactNode } from "react";

interface ThemeProviderProps {
  children: ReactNode;
}

export function ThemeProvider({ children }: ThemeProviderProps) {
  return (
    <NextThemesProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      disableTransitionOnChange={false}
    >
      {children}
    </NextThemesProvider>
  );
}
