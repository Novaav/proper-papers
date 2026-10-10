import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_SESSION_SECONDS,
  sealSession,
  sealTransaction,
  unsealSession,
  unsealTransaction,
} from "./session.ts";

const KEY = { 1: "k".repeat(32) };
const NOW = Math.floor(Date.now() / 1000);
const SESSION = { accessToken: "token", expiresAt: NOW + 900, createdAt: NOW };

test("a sealed session round-trips and its cookie lives as long as the token", async () => {
  const sealed = await sealSession(SESSION, KEY, NOW);
  assert.equal(sealed.maxAge, 900);
  assert.ok(!sealed.value.includes("token"));
  assert.deepEqual(await unsealSession(sealed.value, KEY, NOW), SESSION);
});

test("tampered, foreign-key and garbage cookies are rejected", async () => {
  const { value } = await sealSession(SESSION, KEY, NOW);
  const flipped =
    value.slice(0, 40) + (value[40] === "A" ? "B" : "A") + value.slice(41);
  assert.equal(await unsealSession(flipped, KEY, NOW), null);
  assert.equal(await unsealSession(value, { 1: "x".repeat(32) }, NOW), null);
  assert.equal(await unsealSession("not-a-seal", KEY, NOW), null);
  assert.equal(await unsealSession(undefined, KEY, NOW), null);
});

test("an expired or almost expired session is rejected", async () => {
  const { value } = await sealSession(SESSION, KEY, NOW);
  // Rejected from EXPIRY_MARGIN_SECONDS (10 s) before the token expires.
  assert.ok(await unsealSession(value, KEY, NOW + 889));
  assert.equal(await unsealSession(value, KEY, NOW + 890), null);
  assert.equal(await unsealSession(value, KEY, NOW + 900), null);
});

test("sessions longer than the hard cap cannot be sealed or accepted", async () => {
  await assert.rejects(
    sealSession(
      { ...SESSION, expiresAt: NOW + MAX_SESSION_SECONDS + 1 },
      KEY,
      NOW,
    ),
  );
  await assert.rejects(sealSession({ ...SESSION, expiresAt: NOW }, KEY, NOW));
});

test("key rotation: the old key still unseals, the new key seals", async () => {
  const old = await sealSession(SESSION, { 1: "o".repeat(32) }, NOW);
  const rotated = { 1: "o".repeat(32), 2: "n".repeat(32) };
  assert.deepEqual(await unsealSession(old.value, rotated, NOW), SESSION);
  const fresh = await sealSession(SESSION, rotated, NOW);
  assert.deepEqual(
    await unsealSession(fresh.value, { 2: "n".repeat(32) }, NOW),
    SESSION,
  );
  assert.equal(
    await unsealSession(fresh.value, { 1: "o".repeat(32) }, NOW),
    null,
  );
});

test("a transaction cannot be read as a session and vice versa", async () => {
  const transaction = await sealTransaction(
    { state: "s", nonce: "n", codeVerifier: "v", returnTo: "/" },
    KEY,
  );
  const session = await sealSession(SESSION, KEY, NOW);
  assert.equal(await unsealSession(transaction, KEY, NOW), null);
  assert.equal(await unsealTransaction(session.value, KEY), null);
  assert.deepEqual(await unsealTransaction(transaction, KEY), {
    state: "s",
    nonce: "n",
    codeVerifier: "v",
    returnTo: "/",
  });
});
