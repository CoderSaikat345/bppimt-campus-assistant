"use client";

import { signIn, useSession } from "next-auth/react";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { GraduationCap, LogIn, ShieldCheck } from "lucide-react";

export default function LoginPage() {
  const { data: session, status } = useSession();
  const router = useRouter();

  useEffect(() => {
    if (session) router.replace("/chat");
  }, [session, router]);

  const handleSignIn = () => {
    void signIn("google", { callbackUrl: "/chat" });
  };

  if (status === "loading") {
    return (
      <div className="flex h-screen items-center justify-center bg-surface-50">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-brand-200 border-t-brand-600" />
      </div>
    );
  }

  return (
    <main
      className="flex min-h-screen flex-col items-center justify-center bg-gradient-to-br from-brand-950 via-surface-900 to-surface-950 px-4"
      aria-label="BPPIMT Campus Assistant login page"
    >
      {/* Background decoration */}
      <div aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute -top-32 -right-32 h-96 w-96 rounded-full bg-brand-500/10 blur-3xl" />
        <div className="absolute -bottom-32 -left-32 h-96 w-96 rounded-full bg-brand-700/10 blur-3xl" />
      </div>

      <div className="relative w-full max-w-sm animate-slide-up">
        {/* Card */}
        <div className="rounded-3xl border border-white/10 bg-white/5 backdrop-blur-xl p-8 shadow-2xl">
          {/* Logo */}
          <div className="flex flex-col items-center text-center mb-8">
            <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-400 to-brand-600 shadow-glow-brand mb-5">
              <GraduationCap className="h-8 w-8 text-white" aria-hidden />
            </div>
            <h1 className="text-2xl font-bold text-white">BPPIMT Assistant</h1>
            <p className="mt-2 text-sm text-surface-400 leading-relaxed">
              Your AI-powered campus companion
            </p>
          </div>

          {/* Sign in button */}
          <button
            type="button"
            onClick={handleSignIn}
            aria-label="Sign in with your BPPIMT Google account"
            className={[
              "flex w-full items-center justify-center gap-3 rounded-2xl",
              "bg-white px-5 py-3.5 text-sm font-semibold text-surface-900",
              "hover:bg-surface-50 active:scale-[0.98]",
              "transition-all duration-200 shadow-card-md",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-brand-800",
            ].join(" ")}
          >
            {/* Google icon SVG */}
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              aria-hidden
              role="img"
            >
              <path
                fill="#4285F4"
                d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"
              />
              <path
                fill="#34A853"
                d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
              />
              <path
                fill="#FBBC05"
                d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"
              />
              <path
                fill="#EA4335"
                d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"
              />
            </svg>
            Continue with Google
            <LogIn className="h-4 w-4" aria-hidden />
          </button>

          {/* Domain restriction notice */}
          <div className="mt-5 flex items-start gap-2.5 rounded-xl border border-brand-500/20 bg-brand-500/5 px-3.5 py-3">
            <ShieldCheck className="h-4 w-4 text-brand-400 shrink-0 mt-0.5" aria-hidden />
            <p className="text-xs text-surface-400 leading-relaxed">
              Access restricted exclusively to{" "}
              <span className="font-semibold text-brand-300">@bppimt.ac.in</span>{" "}
              Google accounts.
            </p>
          </div>
        </div>

        <p className="mt-6 text-center text-xs text-surface-600">
          B. P. Poddar Institute of Management &amp; Technology
        </p>
      </div>
    </main>
  );
}
