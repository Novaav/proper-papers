import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import { loadAuthConfig, type AuthConfig } from "./config.ts";
import {
  APP_ORIGIN,
  AUDIENCE,
  CLIENT_ID,
  CLIENT_SECRET,
  FakeIdp,
  ISSUER,
} from "./fake-idp.testsupport.ts";
import {
  handleCallback,
  handleLogin,
  handleLogout,
  type AuthDeps,
} from "./handlers.ts";
import { createOidcClient } from "./oidc.ts";
import {
  SESSION_COOKIE,
  TRANSACTION_COOKIE,
  unsealSession,
} from "./session.ts";

const config: AuthConfig = (() => {
  const result = loadAuthConfig({
    APP_ORIGIN,
    OIDC_ISSUER: ISSUER,
    OIDC_CLIENT_ID: CLIENT_ID,
    OIDC_CLIENT_SECRET: CLIENT_SECRET,
    OIDC_AUDIENCE: AUDIENCE,
    SESSION_SECRET: "session-secret-".repeat(4),
  });
  assert.ok(result.ok);
  return result.config;
})();

let idp: FakeIdp;
let logs: string[];
let deps: AuthDeps;

beforeEach(() => {
  idp = new FakeIdp();
  logs = [];
  deps = {
    config,
    oidc: createOidcClient(config, idp.options),
    now: () => Math.floor(Date.now() / 1000),
    log: (message) => logs.push(message),
  };
});

interface Cookie {
  name: string;
  value: string;
  attributes: Record<string, string | true>;
}

function cookies(response: Response): Map<string, Cookie> {
  const result = new Map<string, Cookie>();
  for (const header of response.headers.getSetCookie()) {
    const [pair = "", ...rest] = header.split(";").map((part) => part.trim());
    const index = pair.indexOf("=");
    const attributes: Record<string, string | true> = {};
    for (const attribute of rest) {
      const [key = "", value] = attribute.split("=");
      attributes[key.toLowerCase()] = value ?? true;
    }
    result.set(pair.slice(0, index), {
      name: pair.slice(0, index),
      value: pair.slice(index + 1),
      attributes,
    });
  }
  return result;
}

function assertHostCookie(
  cookie: Cookie | undefined,
  maxAge: number | "positive",
): void {
  assert.ok(cookie, "cookie is set");
  assert.equal(cookie.attributes.path, "/");
  assert.equal(cookie.attributes.secure, true);
  assert.equal(cookie.attributes.httponly, true);
  assert.equal(String(cookie.attributes.samesite).toLowerCase(), "lax");
  assert.equal(cookie.attributes.domain, undefined);
  if (maxAge === "positive")
    assert.ok(Number(cookie.attributes["max-age"]) > 0);
  else assert.equal(Number(cookie.attributes["max-age"]), maxAge);
}

function assertCleared(response: Response, name: string): void {
  const cookie = cookies(response).get(name);
  assertHostCookie(cookie, 0);
  assert.equal(cookie?.value, "");
}

async function login(returnTo = "/account") {
  const response = await handleLogin(
    new Request(
      `${APP_ORIGIN}/auth/login?returnTo=${encodeURIComponent(returnTo)}`,
    ),
    deps,
  );
  const location = new URL(response.headers.get("location") ?? "");
  idp.authorize(location);
  const transaction = cookies(response).get(TRANSACTION_COOKIE)?.value;
  return { response, location, transaction };
}

function callback(query: string, transaction: string | undefined) {
  return handleCallback(
    new Request(`https://attacker-host.test/auth/callback?${query}`),
    (name) => (name === TRANSACTION_COOKIE ? transaction : undefined),
    deps,
  );
}

// --- login -----------------------------------------------------------------------------------

test("login redirects to the IdP with PKCE S256, state, nonce, audience and openid only", async () => {
  const { response, location, transaction } = await login();

  assert.equal(response.status, 302);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(`${location.origin}${location.pathname}`, `${ISSUER}authorize`);
  const params = location.searchParams;
  assert.equal(params.get("response_type"), "code");
  assert.equal(params.get("client_id"), CLIENT_ID);
  assert.equal(params.get("redirect_uri"), `${APP_ORIGIN}/auth/callback`);
  assert.equal(params.get("scope"), "openid");
  assert.equal(params.get("audience"), AUDIENCE);
  assert.equal(params.get("code_challenge_method"), "S256");
  assert.match(params.get("code_challenge") ?? "", /^[A-Za-z0-9_-]{43}$/);
  assert.ok((params.get("state") ?? "").length >= 32);
  assert.ok((params.get("nonce") ?? "").length >= 32);
  assert.equal(params.has("client_secret"), false);
  // The transaction cookie is sealed and short-lived; any existing session is cleared.
  assertHostCookie(cookies(response).get(TRANSACTION_COOKIE), 600);
  assert.ok(transaction && !transaction.includes(params.get("state") ?? "-"));
  assertCleared(response, SESSION_COOKIE);
});

test("each login gets fresh state, nonce and verifier", async () => {
  const first = await login();
  const second = await login();
  for (const name of ["state", "nonce", "code_challenge"]) {
    assert.notEqual(
      first.location.searchParams.get(name),
      second.location.searchParams.get(name),
    );
  }
});

test("login is unavailable (503, no cookies) without configuration or discovery", async () => {
  const unconfigured = await handleLogin(
    new Request(`${APP_ORIGIN}/auth/login`),
    {
      ...deps,
      config: null,
      oidc: null,
    },
  );
  assert.equal(unconfigured.status, 503);
  assert.deepEqual(unconfigured.headers.getSetCookie(), []);

  idp.discoveryFails = true;
  const down = await handleLogin(new Request(`${APP_ORIGIN}/auth/login`), deps);
  assert.equal(down.status, 503);
  assert.deepEqual(down.headers.getSetCookie(), []);
  assert.deepEqual(logs, ["auth login failed: discovery_failed"]);
  // Discovery failures are not cached.
  idp.discoveryFails = false;
  assert.equal(
    (await handleLogin(new Request(`${APP_ORIGIN}/auth/login`), deps)).status,
    302,
  );
});

// --- callback: success -----------------------------------------------------------------------

test("a valid callback creates a new session and redirects to the saved path", async () => {
  const { location, transaction } = await login("/account");
  const state = location.searchParams.get("state");

  const response = await callback(
    `code=${idp.code}&state=${state}`,
    transaction,
  );

  assert.equal(response.status, 303);
  assert.equal(response.headers.get("location"), `${APP_ORIGIN}/account`);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assertCleared(response, TRANSACTION_COOKIE);
  const session = cookies(response).get(SESSION_COOKIE);
  assertHostCookie(session, 900);
  assert.ok(session && !session.value.includes("access-token-value"));
  const unsealed = await unsealSession(
    session?.value,
    config.sessionPasswords,
    deps.now(),
  );
  assert.equal(unsealed?.accessToken, "access-token-value");
  assert.equal((unsealed?.expiresAt ?? 0) - (unsealed?.createdAt ?? 0), 900);
  // The code was exchanged with the PKCE verifier and client_secret_post.
  const request = idp.tokenRequests[0];
  assert.equal(request?.get("grant_type"), "authorization_code");
  assert.ok(request?.get("code_verifier"));
  assert.equal(logs.length, 0);
});

test("the session never outlives the hard cap, even if the IdP token does", async () => {
  idp.overrides = { body: { expires_in: 86_400 } };
  const { location, transaction } = await login();
  const response = await callback(
    `code=${idp.code}&state=${location.searchParams.get("state")}`,
    transaction,
  );
  assertHostCookie(cookies(response).get(SESSION_COOKIE), 3600);
});

test("the callback URL is built from APP_ORIGIN, not the request's host", async () => {
  // The request above arrives at attacker-host.test; the token request still uses APP_ORIGIN.
  const { location, transaction } = await login();
  await callback(
    `code=${idp.code}&state=${location.searchParams.get("state")}`,
    transaction,
  );
  assert.equal(
    idp.tokenRequests[0]?.get("redirect_uri"),
    `${APP_ORIGIN}/auth/callback`,
  );
});

// --- callback: failures ----------------------------------------------------------------------

async function assertCallbackFails(
  query: (state: string) => string,
  expectedLog: string,
  options: { transaction?: "missing" | "foreign" } = {},
) {
  const { location, transaction } = await login();
  const other =
    options.transaction === "foreign" ? (await login()).transaction : undefined;
  const state = location.searchParams.get("state") ?? "";
  const response = await callback(
    query(state),
    options.transaction === "missing" ? undefined : (other ?? transaction),
  );
  assert.equal(response.status, 400);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.ok(
    !cookies(response).get(SESSION_COOKIE)?.value,
    "no session is created",
  );
  assertCleared(response, SESSION_COOKIE);
  assertCleared(response, TRANSACTION_COOKIE);
  assert.deepEqual(logs, [`auth callback failed: ${expectedLog}`]);
  const body = await response.text();
  assert.ok(!body.includes(idp.code) && !body.includes(state));
}

test("missing transaction cookie", () =>
  assertCallbackFails(
    (s) => `code=${idp.code}&state=${s}`,
    "missing_or_invalid_transaction",
    {
      transaction: "missing",
    },
  ));

test("tampered transaction cookie", async () => {
  const { location, transaction } = await login();
  const tampered = `${transaction?.slice(0, -4)}AAAA`;
  const response = await callback(
    `code=${idp.code}&state=${location.searchParams.get("state")}`,
    tampered,
  );
  assert.equal(response.status, 400);
  assert.deepEqual(logs, [
    "auth callback failed: missing_or_invalid_transaction",
  ]);
});

test("state mismatch", () =>
  assertCallbackFails(
    (s) => `code=${idp.code}&state=${s}x`,
    "token_exchange_failed",
  ));

test("state from another login (transaction of a different browser flow)", () =>
  assertCallbackFails(
    (s) => `code=${idp.code}&state=${s}`,
    "token_exchange_failed",
    {
      transaction: "foreign",
    },
  ));

test("IdP error response", () =>
  assertCallbackFails((s) => `error=access_denied&state=${s}`, "idp_error"));

test("missing code", () =>
  assertCallbackFails((s) => `state=${s}`, "token_exchange_failed"));

test("code rejected by the IdP", () =>
  assertCallbackFails((s) => `code=wrong&state=${s}`, "token_exchange_failed"));

for (const [label, overrides, reason] of [
  [
    "nonce mismatch",
    { idTokenClaims: { nonce: "other" } },
    "token_exchange_failed",
  ],
  [
    "missing nonce",
    { idTokenClaims: { nonce: undefined } },
    "token_exchange_failed",
  ],
  [
    "wrong issuer",
    { idTokenClaims: { iss: "https://evil.test/" } },
    "token_exchange_failed",
  ],
  [
    "wrong audience",
    { idTokenClaims: { aud: "other-client" } },
    "token_exchange_failed",
  ],
  [
    "expired ID token",
    { idTokenClaims: { exp: 1_000_000, iat: 999_000 } },
    "token_exchange_failed",
  ],
  ["alg none", { idTokenHeader: { alg: "none" } }, "token_exchange_failed"],
  ["alg HS256", { idTokenHeader: { alg: "HS256" } }, "token_exchange_failed"],
  ["no ID token", { omitIdToken: true }, "token_exchange_failed"],
  [
    "no access token",
    { body: { access_token: undefined } },
    "token_exchange_failed",
  ],
  [
    "non-bearer token",
    { body: { token_type: "DPoP" } },
    "unexpected_token_type",
  ],
  [
    "no expires_in",
    { body: { expires_in: undefined } },
    "unusable_token_lifetime",
  ],
  ["tiny expires_in", { body: { expires_in: 5 } }, "unusable_token_lifetime"],
  ["token endpoint error", { status: 500 }, "token_exchange_failed"],
] as const) {
  test(`token response: ${label}`, () => {
    idp.overrides = overrides;
    return assertCallbackFails((s) => `code=${idp.code}&state=${s}`, reason);
  });
}

test("a transaction can be used only once in practice: the callback always clears it", async () => {
  const { location, transaction } = await login();
  const ok = await callback(
    `code=${idp.code}&state=${location.searchParams.get("state")}`,
    transaction,
  );
  assert.equal(ok.status, 303);
  assertCleared(ok, TRANSACTION_COOKIE);
});

test("an unsafe saved returnTo still redirects to /", async () => {
  const { location, transaction } = await login("//evil.test");
  const response = await callback(
    `code=${idp.code}&state=${location.searchParams.get("state")}`,
    transaction,
  );
  assert.equal(response.headers.get("location"), `${APP_ORIGIN}/`);
});

// --- logout ----------------------------------------------------------------------------------

function logoutRequest(origin: string | null): Request {
  const headers: Record<string, string> =
    origin === null ? {} : { Origin: origin };
  return new Request(`${APP_ORIGIN}/auth/logout`, { method: "POST", headers });
}

test("logout from our origin clears both cookies and ends the IdP session", async () => {
  const response = await handleLogout(logoutRequest(APP_ORIGIN), deps);

  assert.equal(response.status, 303);
  const location = new URL(response.headers.get("location") ?? "");
  assert.equal(
    `${location.origin}${location.pathname}`,
    `${ISSUER}oidc/logout`,
  );
  assert.equal(location.searchParams.get("client_id"), CLIENT_ID);
  assert.equal(
    location.searchParams.get("post_logout_redirect_uri"),
    `${APP_ORIGIN}/`,
  );
  assert.equal(location.searchParams.has("id_token_hint"), false);
  assertCleared(response, SESSION_COOKIE);
  assertCleared(response, TRANSACTION_COOKIE);
});

for (const origin of [
  null,
  "https://evil.test",
  "https://app.test.evil.test",
  "null",
  `${APP_ORIGIN}/`,
]) {
  test(`logout with Origin ${String(origin)} is refused and clears nothing`, async () => {
    const response = await handleLogout(logoutRequest(origin), deps);
    assert.equal(response.status, 403);
    assert.deepEqual(response.headers.getSetCookie(), []);
    assert.deepEqual(logs, ["auth logout refused: origin_mismatch"]);
  });
}

test("logout still clears locally when the IdP logout endpoint is unavailable", async () => {
  idp.endSession = false;
  const response = await handleLogout(logoutRequest(APP_ORIGIN), deps);
  assert.equal(response.status, 303);
  assert.equal(response.headers.get("location"), `${APP_ORIGIN}/`);
  assertCleared(response, SESSION_COOKIE);
  assert.deepEqual(logs, [
    "auth logout: idp logout unavailable: no_end_session_endpoint",
  ]);
});

test("logout without configuration still clears the session", async () => {
  const response = await handleLogout(logoutRequest(null), {
    ...deps,
    config: null,
    oidc: null,
  });
  assert.equal(response.status, 303);
  assert.equal(response.headers.get("location"), "/");
  assertCleared(response, SESSION_COOKIE);
});

test("logs never contain codes, state, tokens or secrets", async () => {
  idp.overrides = { idTokenClaims: { nonce: "bad" } };
  const { location, transaction } = await login();
  await callback(
    `code=${idp.code}&state=${location.searchParams.get("state")}`,
    transaction,
  );
  const text = logs.join("\n");
  for (const secret of [
    idp.code,
    location.searchParams.get("state") ?? "-",
    CLIENT_SECRET,
    "access-token-value",
  ]) {
    assert.ok(!text.includes(secret));
  }
});
