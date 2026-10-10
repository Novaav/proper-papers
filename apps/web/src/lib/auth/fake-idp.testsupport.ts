// Test support only (never imported by application code): an in-process OIDC
// provider reached through openid-client's customFetch, so the real library
// validates real responses without network access.

import { createHash, generateKeyPairSync, sign } from "node:crypto";

import * as client from "openid-client";

export const ISSUER = "https://idp.test/";
export const CLIENT_ID = "web-client";
export const CLIENT_SECRET = "web-client-secret-value";
export const AUDIENCE = "https://api.test";
export const APP_ORIGIN = "https://app.test";

const { privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });

const b64u = (data: Buffer | string) => Buffer.from(data).toString("base64url");

export function jwt(
  header: Record<string, unknown>,
  claims: Record<string, unknown>,
): string {
  const input = `${b64u(JSON.stringify(header))}.${b64u(JSON.stringify(claims))}`;
  if (header.alg === "none") return `${input}.`;
  return `${input}.${b64u(sign("sha256", Buffer.from(input), privateKey))}`;
}

export interface TokenOverrides {
  idTokenClaims?: Record<string, unknown>;
  idTokenHeader?: Record<string, unknown>;
  omitIdToken?: boolean;
  body?: Record<string, unknown>;
  status?: number;
}

export class FakeIdp {
  challenge: string | undefined;
  nonce: string | undefined;
  tokenRequests: URLSearchParams[] = [];
  overrides: TokenOverrides = {};
  discoveryFails = false;
  endSession = true;
  code = "code-123";

  /** Records the PKCE challenge and nonce the client sent to /authorize. */
  authorize(url: URL): void {
    this.challenge = url.searchParams.get("code_challenge") ?? undefined;
    this.nonce = url.searchParams.get("nonce") ?? undefined;
  }

  get options(): client.DiscoveryRequestOptions {
    return { [client.customFetch]: (url, init) => this.fetch(url, init) };
  }

  private async fetch(
    url: string,
    init: client.CustomFetchOptions,
  ): Promise<Response> {
    if (url === `${ISSUER}.well-known/openid-configuration`) {
      if (this.discoveryFails) return new Response("down", { status: 503 });
      return Response.json({
        issuer: ISSUER,
        authorization_endpoint: `${ISSUER}authorize`,
        token_endpoint: `${ISSUER}oauth/token`,
        jwks_uri: `${ISSUER}.well-known/jwks.json`,
        ...(this.endSession
          ? { end_session_endpoint: `${ISSUER}oidc/logout` }
          : {}),
        id_token_signing_alg_values_supported: ["RS256"],
        code_challenge_methods_supported: ["S256"],
        response_types_supported: ["code"],
      });
    }
    if (url === `${ISSUER}oauth/token` && init.method === "POST") {
      const body = new URLSearchParams(String(init.body));
      this.tokenRequests.push(body);
      const verifier = body.get("code_verifier") ?? "";
      const pkceOk =
        this.challenge !== undefined &&
        createHash("sha256").update(verifier).digest("base64url") ===
          this.challenge;
      if (
        body.get("client_id") !== CLIENT_ID ||
        body.get("client_secret") !== CLIENT_SECRET ||
        body.get("code") !== this.code ||
        body.get("redirect_uri") !== `${APP_ORIGIN}/auth/callback` ||
        !pkceOk
      ) {
        return Response.json({ error: "invalid_grant" }, { status: 400 });
      }
      const now = Math.floor(Date.now() / 1000);
      const idToken = jwt(
        {
          alg: "RS256",
          typ: "JWT",
          kid: "k1",
          ...this.overrides.idTokenHeader,
        },
        {
          iss: ISSUER,
          aud: CLIENT_ID,
          sub: "auth0|user-1",
          iat: now,
          exp: now + 300,
          nonce: this.nonce,
          ...this.overrides.idTokenClaims,
        },
      );
      return Response.json(
        {
          access_token: "access-token-value",
          token_type: "Bearer",
          expires_in: 900,
          ...(this.overrides.omitIdToken ? {} : { id_token: idToken }),
          ...this.overrides.body,
        },
        { status: this.overrides.status ?? 200 },
      );
    }
    return new Response("not found", { status: 404 });
  }
}
