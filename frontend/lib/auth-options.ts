/**
 * lib/auth-options.ts
 *
 * NextAuth configuration extracted from the route handler to prevent
 * Next.js App Router build errors. Route files can only export HTTP
 * method handlers (GET, POST, etc.) — exporting `authOptions` alongside
 * them causes a type constraint violation.
 *
 * This file is imported by:
 *   - app/api/auth/[...nextauth]/route.ts  (for the handler)
 *   - app/page.tsx, app/chat/page.tsx       (for getServerSession)
 *   - app/api/chat/stream/route.ts          (for getServerSession)
 */

import type { AuthOptions, Profile } from "next-auth";
import GoogleProvider from "next-auth/providers/google";
import type { JWT } from "next-auth/jwt";
import type { Session, Account } from "next-auth";

// ---------------------------------------------------------------------------
// Runtime environment assertions
// ---------------------------------------------------------------------------
function requireEnv(key: string): string {
  const value = process.env[key];
  if (!value || value.trim() === "") {
    throw new Error(
      `[Auth] Missing required environment variable: ${key}. ` +
        `Ensure it is set in .env.local (development) or as a Cloud Run secret (production).`
    );
  }
  return value;
}

const GOOGLE_CLIENT_ID = requireEnv("GOOGLE_CLIENT_ID");
const GOOGLE_CLIENT_SECRET = requireEnv("GOOGLE_CLIENT_SECRET");
const NEXTAUTH_SECRET = requireEnv("NEXTAUTH_SECRET");

const ALLOWED_DOMAIN = "bppimt.ac.in" as const;
const GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function extractDomain(email: string | null | undefined): string | null {
  if (!email || !email.includes("@")) return null;
  return email.split("@")[1].toLowerCase().trim();
}

async function refreshGoogleToken(
  refreshToken: string
): Promise<{
  idToken: string;
  accessToken: string;
  expiresAt: number;
  refreshToken: string;
}> {
  const response = await fetch(GOOGLE_TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: GOOGLE_CLIENT_ID,
      client_secret: GOOGLE_CLIENT_SECRET,
      grant_type: "refresh_token",
      refresh_token: refreshToken,
    }),
  });

  const data = await response.json();

  if (!response.ok) {
    console.error("[Auth] Failed to refresh Google token:", data);
    throw new Error(`Token refresh failed: ${data.error ?? "unknown error"}`);
  }

  return {
    idToken: data.id_token,
    accessToken: data.access_token,
    expiresAt: Math.floor(Date.now() / 1000) + (data.expires_in as number),
    refreshToken: (data.refresh_token as string) ?? refreshToken,
  };
}

// ---------------------------------------------------------------------------
// AuthOptions
// ---------------------------------------------------------------------------
export const authOptions: AuthOptions = {
  session: {
    strategy: "jwt",
    maxAge: 24 * 60 * 60,
  },

  providers: [
    GoogleProvider({
      clientId: GOOGLE_CLIENT_ID,
      clientSecret: GOOGLE_CLIENT_SECRET,
      authorization: {
        params: {
          hd: ALLOWED_DOMAIN,
          prompt: "select_account",
          access_type: "offline",
          scope: "openid email profile",
          response_type: "code",
        },
      },
    }),
  ],

  callbacks: {
    async signIn({
      account,
      profile,
    }: {
      account: Account | null;
      profile?: Profile;
    }): Promise<boolean | string> {
      if (account?.provider !== "google") {
        console.warn(`[Auth] Non-Google provider rejected: ${account?.provider}`);
        return false;
      }

      const email = profile?.email ?? null;
      const claimedHd = (profile as Profile & { hd?: string })?.hd ?? null;
      const parsedDomain = extractDomain(email);

      if (claimedHd !== ALLOWED_DOMAIN) {
        console.warn(
          `[Auth] REJECTED — hd claim "${claimedHd}" does not match required domain "${ALLOWED_DOMAIN}". Email: ${email}`
        );
        return "/login?error=DomainNotAllowed";
      }

      if (parsedDomain !== ALLOWED_DOMAIN) {
        console.warn(
          `[Auth] REJECTED — parsed email domain "${parsedDomain}" does not match "${ALLOWED_DOMAIN}". Full email: ${email}`
        );
        return "/login?error=DomainNotAllowed";
      }

      if (!email?.endsWith(`@${ALLOWED_DOMAIN}`)) {
        console.warn(
          `[Auth] REJECTED — email "${email}" does not end with @${ALLOWED_DOMAIN}.`
        );
        return "/login?error=DomainNotAllowed";
      }

      console.info(`[Auth] ACCEPTED — verified domain user: ${email}`);
      return true;
    },

    async jwt({
      token,
      account,
      profile,
    }: {
      token: JWT;
      account: Account | null;
      profile?: Profile;
    }): Promise<JWT> {
      if (account && profile) {
        if (!account.id_token) {
          throw new Error(
            "[Auth] Google did not return an id_token. Ensure 'openid' is in the OAuth scope."
          );
        }

        token.idToken = account.id_token;
        token.accessToken = account.access_token ?? "";
        token.refreshToken = account.refresh_token ?? "";
        token.expiresAt = account.expires_at ?? 0;
        token.userId = account.providerAccountId;
        token.email = profile.email ?? "";
        token.domain = extractDomain(profile.email) ?? "";
        token.error = undefined;

        return token;
      }

      const REFRESH_BUFFER_SECONDS = 300;
      const nowEpoch = Math.floor(Date.now() / 1000);

      if (
        typeof token.expiresAt === "number" &&
        token.expiresAt > 0 &&
        nowEpoch < token.expiresAt - REFRESH_BUFFER_SECONDS
      ) {
        return token;
      }

      if (!token.refreshToken) {
        console.error(
          `[Auth] ID token expired but no refresh_token available. User: ${token.email}`
        );
        token.error = "RefreshTokenMissing";
        return token;
      }

      try {
        const refreshed = await refreshGoogleToken(token.refreshToken as string);

        token.idToken = refreshed.idToken;
        token.accessToken = refreshed.accessToken;
        token.expiresAt = refreshed.expiresAt;
        token.refreshToken = refreshed.refreshToken;
        token.error = undefined;

        console.info(`[Auth] Successfully refreshed ID token for ${token.email}`);
      } catch (error) {
        console.error(
          `[Auth] Failed to refresh token for ${token.email}:`,
          error
        );
        token.error = "RefreshTokenError";
      }

      return token;
    },

    async session({
      session,
      token,
    }: {
      session: Session;
      token: JWT;
    }): Promise<Session> {
      session.idToken = token.idToken;
      session.user.id = token.userId;
      session.user.email = token.email;
      session.user.domain = token.domain;

      if (token.error) {
        (session as Session & { error?: string }).error = token.error as string;
      }

      return session;
    },
  },

  pages: {
    signIn: "/login",
    error: "/login",
  },

  events: {
    async signIn({ user }) {
      console.info(`[Auth:Event] signIn  user=${user.email}`);
    },
    async signOut({ token }) {
      console.info(`[Auth:Event] signOut user=${(token as JWT).email}`);
    },
  },

  secret: NEXTAUTH_SECRET,
  debug: process.env.NODE_ENV !== "production",
};
