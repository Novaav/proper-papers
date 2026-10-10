// The OIDC relying party (Authorization Code + PKCE, confidential client),
// built on openid-client. Discovery runs on first use and is cached; a failed
// discovery is not cached, so the next request retries.

import * as client from "openid-client";

import type { AuthConfig } from "./config.ts";

/** Accepted lifetime of the IdP access token, in seconds. */
export const MIN_TOKEN_SECONDS = 30;

export interface TokenResult {
  accessToken: string;
  expiresIn: number;
}

export interface AuthorizationRequest {
  url: URL;
  state: string;
  nonce: string;
  codeVerifier: string;
}

export interface OidcClient {
  startAuthorization(): Promise<AuthorizationRequest>;
  /** Validates the callback (state, PKCE, ID token claims, nonce) and returns the access token. */
  finishAuthorization(
    callbackUrl: URL,
    expected: { state: string; nonce: string; codeVerifier: string },
  ): Promise<TokenResult>;
  endSessionUrl(postLogoutRedirectUri: string): Promise<URL>;
}

export class OidcError extends Error {
  readonly category: string;

  constructor(category: string) {
    super(`oidc: ${category}`);
    this.category = category;
  }
}

/** Test hook only: options passed to openid-client discovery (e.g. customFetch). */
export type DiscoveryOptions = client.DiscoveryRequestOptions;

export function createOidcClient(
  config: AuthConfig,
  discoveryOptions?: DiscoveryOptions,
): OidcClient {
  let discovered: Promise<client.Configuration> | undefined;

  const configuration = (): Promise<client.Configuration> => {
    discovered ??= client
      .discovery(
        config.issuer,
        config.clientId,
        // RS256 is the only accepted ID token algorithm (also openid-client's default).
        { id_token_signed_response_alg: "RS256" },
        client.ClientSecretPost(config.clientSecret),
        discoveryOptions,
      )
      .catch((error: unknown) => {
        discovered = undefined;
        throw error;
      });
    return discovered;
  };

  return {
    async startAuthorization() {
      const oidc = await wrap("discovery_failed", configuration);
      const codeVerifier = client.randomPKCECodeVerifier();
      const state = client.randomState();
      const nonce = client.randomNonce();
      const url = client.buildAuthorizationUrl(oidc, {
        redirect_uri: config.redirectUri,
        scope: "openid",
        audience: config.audience,
        state,
        nonce,
        code_challenge: await client.calculatePKCECodeChallenge(codeVerifier),
        code_challenge_method: "S256",
      });
      return { url, state, nonce, codeVerifier };
    },

    async finishAuthorization(callbackUrl, expected) {
      const oidc = await wrap("discovery_failed", configuration);
      const tokens = await wrap("token_exchange_failed", () =>
        client.authorizationCodeGrant(oidc, callbackUrl, {
          pkceCodeVerifier: expected.codeVerifier,
          expectedState: expected.state,
          expectedNonce: expected.nonce,
          idTokenExpected: true,
        }),
      );
      const accessToken = tokens.access_token;
      const expiresIn = tokens.expires_in;
      if (typeof accessToken !== "string" || accessToken.length === 0) {
        throw new OidcError("no_access_token");
      }
      if (tokens.token_type.toLowerCase() !== "bearer") {
        throw new OidcError("unexpected_token_type");
      }
      if (
        typeof expiresIn !== "number" ||
        !Number.isFinite(expiresIn) ||
        expiresIn < MIN_TOKEN_SECONDS
      ) {
        throw new OidcError("unusable_token_lifetime");
      }
      return { accessToken, expiresIn: Math.floor(expiresIn) };
    },

    async endSessionUrl(postLogoutRedirectUri) {
      const oidc = await wrap("discovery_failed", configuration);
      return wrap("no_end_session_endpoint", async () =>
        client.buildEndSessionUrl(oidc, {
          client_id: config.clientId,
          post_logout_redirect_uri: postLogoutRedirectUri,
        }),
      );
    },
  };
}

async function wrap<T>(category: string, run: () => Promise<T>): Promise<T> {
  try {
    return await run();
  } catch (error) {
    if (error instanceof OidcError) throw error;
    throw new OidcError(category);
  }
}
