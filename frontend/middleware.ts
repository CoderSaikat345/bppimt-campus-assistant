/**
 * middleware.ts  (Next.js Edge Middleware)
 *
 * Protects all routes under /(protected) at the edge — before the request
 * reaches the React Server Component render pipeline.
 *
 * Behaviour:
 *   - Unauthenticated requests are redirected to /login.
 *   - The /login page and /api/auth/* routes are always public.
 *   - Static assets and Next.js internals are bypassed via the `matcher`.
 *
 * This is the third authentication layer on the frontend — it prevents
 * page content from being server-rendered for unauthenticated users even
 * if a route guard in a Server Component is somehow bypassed.
 */

import { withAuth } from "next-auth/middleware";
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

export default withAuth(
  function middleware(req: NextRequest) {
    // At this point `withAuth` has already verified the JWT is present and valid.
    // We can perform additional checks here if needed (e.g. role-based access).
    return NextResponse.next();
  },
  {
    callbacks: {
      /**
       * Return true to allow the request, false to redirect to the sign-in page.
       * `token` is null if the user has no valid session cookie.
       */
      authorized({ token }) {
        return !!token;
      },
    },
    pages: {
      signIn: "/login",
    },
  }
);

/**
 * Matcher: apply middleware only to application routes.
 * Excludes: _next internals, static files, public assets, and auth API routes.
 */
export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|login|api/auth).*)",
  ],
};
