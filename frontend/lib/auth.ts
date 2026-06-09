/**
 * lib/auth.ts
 *
 * Single re-export point for authOptions.
 * Server Components and Route Handlers import from here instead of from
 * the [...nextauth] route file, avoiding the App Router handler binding.
 *
 * Usage:
 *   import { authOptions } from "@/lib/auth";
 *   const session = await getServerSession(authOptions);
 */

export { authOptions } from "@/lib/auth-options";
