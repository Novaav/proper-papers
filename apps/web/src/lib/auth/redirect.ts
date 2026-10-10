// Where to send the user after login: only a same-origin relative path.

const FALLBACK = "/";
// A leading "/" not followed by "/" or "\" (protocol-relative or browser-
// normalised to one), then only unreserved path characters. No query,
// fragment, percent-encoding, control characters or backslashes.
const SAFE_PATH = /^\/(?![/\\])[A-Za-z0-9/_.\-]*$/;
const MAX_LENGTH = 512;

export function safeReturnTo(value: string | null | undefined): string {
  if (typeof value !== "string" || value.length > MAX_LENGTH) return FALLBACK;
  if (!SAFE_PATH.test(value)) return FALLBACK;
  // Dot segments could climb out of the intended path; reject them.
  if (value.split("/").some((segment) => segment === "." || segment === "..")) {
    return FALLBACK;
  }
  return value;
}

/**
 * On an API 401, re-authenticate only if the session is not brand new. A token
 * the API rejects right after login would otherwise loop through the IdP
 * (silent re-login, rejected again); that case is shown as an error instead.
 */
export const FRESH_SESSION_SECONDS = 60;

export function reauthenticateOnUnauthorized(
  session: { createdAt: number },
  now: number,
): boolean {
  return now - session.createdAt >= FRESH_SESSION_SECONDS;
}
