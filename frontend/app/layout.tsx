import type { Metadata, Viewport } from "next";
import "./globals.css";
import { SessionProvider } from "../components/session-provider";
import { ThemeProvider } from "../components/theme-provider";

export const metadata: Metadata = {
  title: "BPPIMT Campus Resource Assistant",
  description:
    "AI-powered campus assistant for B. P. Poddar Institute of Management & Technology — access timetables, fees, faculty schedules, and more.",
  keywords: ["BPPIMT", "campus assistant", "timetable", "AI chatbot", "university"],
  authors: [{ name: "BPPIMT Tech Team" }],
  openGraph: {
    title: "BPPIMT Campus Resource Assistant",
    description: "Your AI campus companion at BPPIMT",
    type: "website",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#3b67f3",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className="h-full" suppressHydrationWarning>
      <body className="h-full antialiased">
        <ThemeProvider>
          <SessionProvider>{children}</SessionProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
