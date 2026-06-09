/**
 * app/api/auth/[...nextauth]/route.ts
 *
 * Next.js App Router handler for NextAuth.
 * Auth configuration is in lib/auth-options.ts to avoid exporting
 * non-handler values from route files (which causes build errors).
 */

import NextAuth from "next-auth";
import { authOptions } from "@/lib/auth-options";

const handler = NextAuth(authOptions);

export { handler as GET, handler as POST };
