// Content-Security-Policy for every page, with a fresh nonce per request
// (Next.js applies the nonce to its own scripts during dynamic rendering).
// No 'unsafe-inline' anywhere; 'unsafe-eval' only in development, where
// React needs it for error overlays.

export interface CspOptions {
  nonce: string;
  development: boolean;
  /** OIDC_ISSUER; its origin is allowed as a form target (see formAction). */
  issuer: string | undefined;
}

export function buildContentSecurityPolicy({
  nonce,
  development,
  issuer,
}: CspOptions): string {
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${development ? " 'unsafe-eval'" : ""}`,
    `style-src 'self' 'nonce-${nonce}'`,
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    formAction(issuer),
  ].join("; ");
}

/**
 * The sign-out form posts to /auth/logout, which redirects to the provider's
 * end-session endpoint. Browsers (Chrome) apply form-action to redirects that
 * follow a form submission, so the issuer's origin must be allowed or
 * federated sign-out would be blocked. Only an https issuer is added.
 */
function formAction(issuer: string | undefined): string {
  const origin = httpsOrigin(issuer);
  return origin ? `form-action 'self' ${origin}` : "form-action 'self'";
}

function httpsOrigin(value: string | undefined): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value.trim());
    return url.protocol === "https:" && !url.username && !url.password
      ? url.origin
      : undefined;
  } catch {
    return undefined;
  }
}

/** 128 random bits, base64: unguessable and unique per request. */
export function generateNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Buffer.from(bytes).toString("base64");
}
