import type { Session } from "next-auth";
import type { JWT } from "next-auth/jwt";

/**
 * Augment the built-in NextAuth types so every downstream component and API
 * route receives fully typed session and JWT objects without casting.
 *
 * Rule: every field added here MUST be populated inside the `jwt` and
 * `session` callbacks in route.ts — keep them in sync.
 */
declare module "next-auth" {
  interface Session {
    /** Raw Google ID token forwarded to the FastAPI backend as a Bearer token. */
    idToken: string;
    user: {
      id: string;
      name: string;
      email: string;
      image: string;
      /** Verified to be "bppimt.ac.in" — enforced at sign-in callback level. */
      domain: string;
    };
    /** Present when a token refresh error occurred. */
    error?: string;
  }

  interface Profile {
    /** Google's hosted-domain claim — present when hd is configured. */
    hd?: string;
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    idToken: string;
    accessToken: string;
    refreshToken: string;
    expiresAt: number;
    userId: string;
    domain: string;
    email: string;
    /** Present when a token refresh error occurred. */
    error?: string;
  }
}
