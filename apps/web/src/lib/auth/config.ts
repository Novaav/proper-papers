// Web authentication configuration, read from server-only environment
// variables. Values are never logged; errors name the variable only.

export interface AuthConfig {
  /** Public origin of this web app, e.g. https://localhost:3000 (no path). */
  appOrigin: string;
  /** OIDC issuer URL exactly as in tokens, e.g. https://tenant.eu.auth0.com/ */
  issuer: URL;
  clientId: string;
  clientSecret: string;
  /** API identifier the access token is requested for (Auth0 `audience`). */
  audience: string;
  /** iron-session password map; the highest id seals, all ids unseal. */
  sessionPasswords: Record<string, string>;
  redirectUri: string;
}

export type AuthConfigResult =
  { ok: true; config: AuthConfig } | { ok: false; errors: string[] };

type Env = Readonly<Record<string, string | undefined>>;

const MIN_SECRET_LENGTH = 32;

export function loadAuthConfig(env: Env): AuthConfigResult {
  const errors: string[] = [];
  const required = (name: string): string => {
    const value = env[name]?.trim() ?? "";
    if (!value) errors.push(`${name} is not set`);
    return value;
  };

  const appOrigin = required("APP_ORIGIN");
  const issuer = required("OIDC_ISSUER");
  const clientId = required("OIDC_CLIENT_ID");
  const clientSecret = required("OIDC_CLIENT_SECRET");
  const audience = required("OIDC_AUDIENCE");
  const secret = required("SESSION_SECRET");
  const previous = env.SESSION_SECRET_PREVIOUS?.trim() ?? "";

  if (appOrigin && !isHttpsOrigin(appOrigin)) {
    errors.push("APP_ORIGIN must be an https origin without a path");
  }
  let issuerUrl: URL | undefined;
  if (issuer) {
    issuerUrl = parseHttpsUrl(issuer);
    if (!issuerUrl) errors.push("OIDC_ISSUER must be an https URL");
  }
  if (secret && secret.length < MIN_SECRET_LENGTH) {
    errors.push(
      `SESSION_SECRET must be at least ${MIN_SECRET_LENGTH} characters`,
    );
  }
  if (previous && previous.length < MIN_SECRET_LENGTH) {
    errors.push(
      `SESSION_SECRET_PREVIOUS must be at least ${MIN_SECRET_LENGTH} characters`,
    );
  }
  if (previous && previous === secret) {
    errors.push("SESSION_SECRET_PREVIOUS must differ from SESSION_SECRET");
  }

  if (errors.length > 0 || !issuerUrl) {
    return { ok: false, errors };
  }
  return {
    ok: true,
    config: {
      appOrigin,
      issuer: issuerUrl,
      clientId,
      clientSecret,
      audience,
      sessionPasswords: previous ? { 1: previous, 2: secret } : { 1: secret },
      redirectUri: `${appOrigin}/auth/callback`,
    },
  };
}

function isHttpsOrigin(value: string): boolean {
  const url = parseHttpsUrl(value);
  return url !== undefined && url.origin === value;
}

function parseHttpsUrl(value: string): URL | undefined {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password) {
      return undefined;
    }
    return url;
  } catch {
    return undefined;
  }
}
