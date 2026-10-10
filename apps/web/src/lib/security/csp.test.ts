import assert from "node:assert/strict";
import { test } from "node:test";

import { buildContentSecurityPolicy, generateNonce } from "./csp.ts";

function directives(policy: string): Map<string, string[]> {
  return new Map(
    policy.split("; ").map((entry) => {
      const [name = "", ...values] = entry.split(" ");
      return [name, values];
    }),
  );
}

const ISSUER = "https://tenant.eu.auth0.com/";

test("production policy: nonce + strict-dynamic, no unsafe keywords", () => {
  const policy = buildContentSecurityPolicy({
    nonce: "abc",
    development: false,
    issuer: ISSUER,
  });
  const d = directives(policy);
  assert.deepEqual(d.get("script-src"), [
    "'self'",
    "'nonce-abc'",
    "'strict-dynamic'",
  ]);
  assert.deepEqual(d.get("style-src"), ["'self'", "'nonce-abc'"]);
  assert.deepEqual(d.get("default-src"), ["'self'"]);
  assert.deepEqual(d.get("object-src"), ["'none'"]);
  assert.deepEqual(d.get("base-uri"), ["'none'"]);
  assert.deepEqual(d.get("frame-ancestors"), ["'none'"]);
  assert.ok(!policy.includes("unsafe-inline"));
  assert.ok(!policy.includes("unsafe-eval"));
});

test("development adds only unsafe-eval to script-src", () => {
  const policy = buildContentSecurityPolicy({
    nonce: "abc",
    development: true,
    issuer: ISSUER,
  });
  assert.deepEqual(directives(policy).get("script-src"), [
    "'self'",
    "'nonce-abc'",
    "'strict-dynamic'",
    "'unsafe-eval'",
  ]);
  assert.ok(!policy.includes("unsafe-inline"));
});

test("form-action allows the issuer origin (federated sign-out redirect)", () => {
  const d = directives(
    buildContentSecurityPolicy({
      nonce: "n",
      development: false,
      issuer: ISSUER,
    }),
  );
  assert.deepEqual(d.get("form-action"), [
    "'self'",
    "https://tenant.eu.auth0.com",
  ]);
});

test("form-action falls back to 'self' for a missing or unsafe issuer", () => {
  for (const issuer of [
    undefined,
    "",
    "not a url",
    "http://tenant.test/",
    "https://u:p@tenant.test/",
    "javascript:alert(1)",
  ]) {
    const d = directives(
      buildContentSecurityPolicy({ nonce: "n", development: false, issuer }),
    );
    assert.deepEqual(d.get("form-action"), ["'self'"], String(issuer));
  }
});

test("nonces are 128-bit base64 and unique", () => {
  const nonces = new Set(Array.from({ length: 1000 }, generateNonce));
  assert.equal(nonces.size, 1000);
  for (const nonce of nonces) assert.match(nonce, /^[A-Za-z0-9+/]{22}==$/);
});
