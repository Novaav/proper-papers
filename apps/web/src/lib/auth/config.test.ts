import assert from "node:assert/strict";
import { test } from "node:test";

import { loadAuthConfig } from "./config.ts";

const VALID = {
  APP_ORIGIN: "https://localhost:3000",
  OIDC_ISSUER: "https://tenant.eu.auth0.com/",
  OIDC_CLIENT_ID: "client",
  OIDC_CLIENT_SECRET: "secret",
  OIDC_AUDIENCE: "https://api.example",
  SESSION_SECRET: "s".repeat(32),
};

test("a complete configuration is accepted", () => {
  const result = loadAuthConfig(VALID);
  assert.ok(result.ok);
  assert.equal(
    result.config.redirectUri,
    "https://localhost:3000/auth/callback",
  );
  assert.deepEqual(result.config.sessionPasswords, { 1: "s".repeat(32) });
});

test("a previous session secret unseals while the current one seals", () => {
  const result = loadAuthConfig({
    ...VALID,
    SESSION_SECRET_PREVIOUS: "p".repeat(32),
  });
  assert.ok(result.ok);
  assert.deepEqual(result.config.sessionPasswords, {
    1: "p".repeat(32),
    2: "s".repeat(32),
  });
});

for (const [name, value, message] of [
  ["APP_ORIGIN", "http://localhost:3000", "https origin"],
  ["APP_ORIGIN", "https://localhost:3000/", "https origin"],
  ["APP_ORIGIN", "https://localhost:3000/app", "https origin"],
  ["APP_ORIGIN", "https://user:pw@localhost:3000", "https origin"],
  ["OIDC_ISSUER", "http://tenant.eu.auth0.com/", "https URL"],
  ["OIDC_ISSUER", "not a url", "https URL"],
  ["SESSION_SECRET", "s".repeat(31), "at least 32"],
  ["SESSION_SECRET_PREVIOUS", "p".repeat(31), "at least 32"],
  ["SESSION_SECRET_PREVIOUS", "s".repeat(32), "must differ"],
] as const) {
  test(`${name}=${value.slice(0, 40)} is rejected`, () => {
    const result = loadAuthConfig({ ...VALID, [name]: value });
    assert.equal(result.ok, false);
    assert.ok(
      !result.ok && result.errors.some((e) => e.includes(message)),
      JSON.stringify(result),
    );
  });
}

test("every missing variable is reported by name only", () => {
  const result = loadAuthConfig({});
  assert.equal(result.ok, false);
  const errors = !result.ok ? result.errors : [];
  for (const name of Object.keys(VALID)) {
    assert.ok(errors.includes(`${name} is not set`), name);
  }
  // Values never appear in messages.
  const withValues = loadAuthConfig({
    ...VALID,
    SESSION_SECRET: "short-secret-value",
  });
  assert.ok(
    !withValues.ok &&
      !withValues.errors.join(" ").includes("short-secret-value"),
  );
});
