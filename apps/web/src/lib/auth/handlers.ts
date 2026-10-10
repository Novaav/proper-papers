// Route handler logic for /auth/login, /auth/callback and /auth/logout.
// Every cookie is set or cleared on the returned NextResponse, the supported
// way for route handlers to change cookies.

import { NextResponse } from "next/server.js";

import type { AuthConfig } from "./config.ts";
import { OidcError, type OidcClient } from "./oidc.ts";
import { safeReturnTo } from "./redirect.ts";
import {
  COOKIE_ATTRIBUTES,
  MAX_SESSION_SECONDS,
  SESSION_COOKIE,
  TRANSACTION_COOKIE,
  TRANSACTION_SECONDS,
  sealSession,
  sealTransaction,
  unsealTransaction,
} from "./session.ts";

export interface AuthDeps {
  /** null when the configuration is invalid: auth routes answer 503. */
  config: AuthConfig | null;
  oidc: OidcClient | null;
  /** Epoch seconds. */
  now: () => number;
  /** Receives a fixed category, never tokens, codes or cookie values. */
  log: (message: string) => void;
}

const NO_STORE = { "Cache-Control": "no-store" };

export async function handleLogin(
  request: Request,
  deps: AuthDeps,
): Promise<NextResponse> {
  const { config, oidc } = deps;
  if (!config || !oidc) return unavailable();
  const returnTo = safeReturnTo(
    new URL(request.url).searchParams.get("returnTo"),
  );
  let authorization;
  try {
    authorization = await oidc.startAuthorization();
  } catch (error) {
    deps.log(`auth login failed: ${category(error)}`);
    return unavailable();
  }
  const transaction = await sealTransaction(
    {
      state: authorization.state,
      nonce: authorization.nonce,
      codeVerifier: authorization.codeVerifier,
      returnTo,
    },
    config.sessionPasswords,
  );
  const response = NextResponse.redirect(authorization.url, {
    status: 302,
    headers: NO_STORE,
  });
  // Starting a login ends any existing session (also how pages invalidate one).
  clear(response, SESSION_COOKIE);
  response.cookies.set(TRANSACTION_COOKIE, transaction, {
    ...COOKIE_ATTRIBUTES,
    maxAge: TRANSACTION_SECONDS,
  });
  return response;
}

export async function handleCallback(
  request: Request,
  cookieValue: (name: string) => string | undefined,
  deps: AuthDeps,
): Promise<NextResponse> {
  const { config, oidc } = deps;
  if (!config || !oidc) return unavailable();
  const requestUrl = new URL(request.url);
  // Rebuild the callback URL from the configured origin, never from Host headers.
  const callbackUrl = new URL(
    `${config.appOrigin}/auth/callback${requestUrl.search}`,
  );

  const transaction = await unsealTransaction(
    cookieValue(TRANSACTION_COOKIE),
    config.sessionPasswords,
  );
  if (!transaction)
    return callbackFailed(deps, "missing_or_invalid_transaction");
  if (requestUrl.searchParams.has("error"))
    return callbackFailed(deps, "idp_error");

  let tokens;
  try {
    tokens = await oidc.finishAuthorization(callbackUrl, transaction);
  } catch (error) {
    return callbackFailed(deps, category(error));
  }

  const now = deps.now();
  const lifetime = Math.min(tokens.expiresIn, MAX_SESSION_SECONDS);
  const session = await sealSession(
    {
      accessToken: tokens.accessToken,
      expiresAt: now + lifetime,
      createdAt: now,
    },
    config.sessionPasswords,
    now,
  );
  const response = NextResponse.redirect(
    new URL(safeReturnTo(transaction.returnTo), config.appOrigin),
    { status: 303, headers: NO_STORE },
  );
  clear(response, TRANSACTION_COOKIE);
  // A new cookie value for every login: an earlier (e.g. planted) cookie is replaced.
  response.cookies.set(SESSION_COOKIE, session.value, {
    ...COOKIE_ATTRIBUTES,
    maxAge: session.maxAge,
  });
  return response;
}

export async function handleLogout(
  request: Request,
  deps: AuthDeps,
): Promise<NextResponse> {
  const { config, oidc } = deps;
  const origin = request.headers.get("origin");
  // Without configuration we cannot check the origin; still clear locally.
  if (config && origin !== config.appOrigin) {
    deps.log("auth logout refused: origin_mismatch");
    return new NextResponse("Forbidden", { status: 403, headers: NO_STORE });
  }
  let target: URL | string = config ? `${config.appOrigin}/` : "/";
  if (config && oidc) {
    try {
      target = await oidc.endSessionUrl(`${config.appOrigin}/`);
    } catch (error) {
      // Local logout still happens; the IdP session may remain.
      deps.log(`auth logout: idp logout unavailable: ${category(error)}`);
    }
  }
  const response = config
    ? NextResponse.redirect(target, { status: 303, headers: NO_STORE })
    : new NextResponse(null, {
        status: 303,
        headers: { ...NO_STORE, Location: "/" },
      });
  clear(response, SESSION_COOKIE);
  clear(response, TRANSACTION_COOKIE);
  return response;
}

function callbackFailed(deps: AuthDeps, reason: string): NextResponse {
  deps.log(`auth callback failed: ${reason}`);
  const response = new NextResponse(
    '<!doctype html><title>Sign-in failed</title><p>Sign-in failed. <a href="/auth/login">Try again</a>.</p>',
    {
      status: 400,
      headers: { ...NO_STORE, "Content-Type": "text/html; charset=utf-8" },
    },
  );
  clear(response, SESSION_COOKIE);
  clear(response, TRANSACTION_COOKIE);
  return response;
}

function unavailable(): NextResponse {
  return new NextResponse("Authentication is unavailable.", {
    status: 503,
    headers: { ...NO_STORE, "Content-Type": "text/plain; charset=utf-8" },
  });
}

/** Deletes a __Host- cookie: the deletion must carry the same Secure/Path attributes. */
function clear(response: NextResponse, name: string): void {
  response.cookies.set(name, "", { ...COOKIE_ATTRIBUTES, maxAge: 0 });
}

function category(error: unknown): string {
  return error instanceof OidcError ? error.category : "unexpected_error";
}
