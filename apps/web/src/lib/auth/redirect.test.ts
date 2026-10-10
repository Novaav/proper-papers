import assert from "node:assert/strict";
import { test } from "node:test";

import { reauthenticateOnUnauthorized, safeReturnTo } from "./redirect.ts";

test("same-origin relative paths are kept", () => {
  for (const path of [
    "/",
    "/account",
    "/a/b-c_d.e",
    "/tenants/0192a8f0-7c1e",
  ]) {
    assert.equal(safeReturnTo(path), path);
  }
});

test("anything else falls back to /", () => {
  for (const value of [
    null,
    undefined,
    "",
    "account",
    "//evil.test",
    "//evil.test/account",
    "/\\evil.test",
    "\\\\evil.test",
    "/\\/evil.test",
    "https://evil.test/",
    "javascript:alert(1)",
    "/%2F%2Fevil.test",
    "/account?next=//evil",
    "/account#x",
    "/a/../b",
    "/./account",
    "/account\n",
    "/account\t",
    " /account",
    "/ac count",
    `/${"a".repeat(600)}`,
    "/ünicode",
  ]) {
    assert.equal(safeReturnTo(value), "/", JSON.stringify(value));
  }
});

test("a rejected token triggers re-login only when the session is not brand new", () => {
  assert.equal(reauthenticateOnUnauthorized({ createdAt: 1000 }, 1059), false);
  assert.equal(reauthenticateOnUnauthorized({ createdAt: 1000 }, 1060), true);
});
