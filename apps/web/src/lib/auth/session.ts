// Sealed cookies: the session (access token only) and the short-lived OIDC
// transaction. Sealing is iron-session's (AES-256-CBC + HMAC-SHA-256 "iron"
// format with a TTL); nothing here implements cryptography.

import { sealData, unsealData } from "iron-session";

export const SESSION_COOKIE = "__Host-session";
export const TRANSACTION_COOKIE = "__Host-oidc";

/** Hard upper bound for a session, whatever the IdP says (seconds). */
export const MAX_SESSION_SECONDS = 3600;
/** A session whose token expires within this margin is treated as expired. */
export const EXPIRY_MARGIN_SECONDS = 10;
export const TRANSACTION_SECONDS = 600;

export interface Session {
  accessToken: string;
  /** Epoch seconds when the access token (and the session) expires. */
  expiresAt: number;
  /** Epoch seconds when the session was created. */
  createdAt: number;
}

export interface Transaction {
  state: string;
  nonce: string;
  codeVerifier: string;
  returnTo: string;
}

/**
 * Attributes shared by both cookies: `__Host-` requires Secure, Path=/ and no
 * Domain. SameSite=Lax (not Strict) because the IdP returns with a cross-site
 * top-level GET to /auth/callback.
 */
export const COOKIE_ATTRIBUTES = {
  httpOnly: true,
  secure: true,
  sameSite: "lax",
  path: "/",
} as const;

type Passwords = Record<string, string>;

export async function sealSession(
  session: Session,
  passwords: Passwords,
  now: number,
): Promise<{ value: string; maxAge: number }> {
  const maxAge = session.expiresAt - now;
  if (maxAge <= 0 || maxAge > MAX_SESSION_SECONDS) {
    throw new Error("session lifetime out of range");
  }
  const value = await sealData(
    {
      v: 1,
      at: session.accessToken,
      exp: session.expiresAt,
      iat: session.createdAt,
    },
    { password: passwords, ttl: maxAge },
  );
  return { value, maxAge };
}

/** The session in a cookie value, or null if missing, tampered, stale or expired. */
export async function unsealSession(
  value: string | undefined,
  passwords: Passwords,
  now: number,
): Promise<Session | null> {
  if (!value) return null;
  const data: Record<string, unknown> = await unsealData(value, {
    password: passwords,
    ttl: MAX_SESSION_SECONDS,
  });
  const { v, at, exp, iat } = data;
  if (
    v !== 1 ||
    typeof at !== "string" ||
    at.length === 0 ||
    !Number.isSafeInteger(exp) ||
    !Number.isSafeInteger(iat)
  ) {
    return null;
  }
  const expiresAt = exp as number;
  const createdAt = iat as number;
  if (
    expiresAt - createdAt > MAX_SESSION_SECONDS ||
    createdAt > now + EXPIRY_MARGIN_SECONDS ||
    expiresAt <= now + EXPIRY_MARGIN_SECONDS
  ) {
    return null;
  }
  return { accessToken: at, expiresAt, createdAt };
}

export async function sealTransaction(
  transaction: Transaction,
  passwords: Passwords,
): Promise<string> {
  return sealData(
    { v: 1, ...transaction },
    { password: passwords, ttl: TRANSACTION_SECONDS },
  );
}

export async function unsealTransaction(
  value: string | undefined,
  passwords: Passwords,
): Promise<Transaction | null> {
  if (!value) return null;
  const data: Record<string, unknown> = await unsealData(value, {
    password: passwords,
    ttl: TRANSACTION_SECONDS,
  });
  const { v, state, nonce, codeVerifier, returnTo } = data;
  if (
    v !== 1 ||
    typeof state !== "string" ||
    typeof nonce !== "string" ||
    typeof codeVerifier !== "string" ||
    typeof returnTo !== "string" ||
    !state ||
    !nonce ||
    !codeVerifier
  ) {
    return null;
  }
  return { state, nonce, codeVerifier, returnTo };
}
