// Server-side entry point for authentication: configuration from the
// environment, the cached OIDC client, and reading the session in Server
// Components. Server Components only READ the session; every change to a
// cookie happens in a route handler response (see handlers.ts).
import "server-only";

import { cookies } from "next/headers.js";

import { createOnceLogger } from "../log-once.ts";
import { loadAuthConfig } from "./config.ts";
import type { AuthDeps } from "./handlers.ts";
import { createOidcClient, type OidcClient } from "./oidc.ts";
import { SESSION_COOKIE, unsealSession, type Session } from "./session.ts";

let cachedOidc: OidcClient | undefined;
const logConfigErrorOnce = createOnceLogger(console.error);

export function authDeps(): AuthDeps {
  const result = loadAuthConfig(process.env);
  if (!result.ok) {
    logConfigErrorOnce(
      `authentication is not configured: ${result.errors.join("; ")}`,
    );
    return { config: null, oidc: null, now: nowSeconds, log: console.warn };
  }
  cachedOidc ??= createOidcClient(result.config);
  return {
    config: result.config,
    oidc: cachedOidc,
    now: nowSeconds,
    log: console.warn,
  };
}

/** The current session, or null. Never modifies cookies. */
export async function readSession(): Promise<Session | null> {
  const result = loadAuthConfig(process.env);
  if (!result.ok) return null;
  const store = await cookies();
  return unsealSession(
    store.get(SESSION_COOKIE)?.value,
    result.config.sessionPasswords,
    nowSeconds(),
  );
}

/** Where a page sends the browser to (re)authenticate; the login route clears any session. */
export function loginPath(returnTo: string): string {
  return `/auth/login?returnTo=${encodeURIComponent(returnTo)}`;
}

export function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}
