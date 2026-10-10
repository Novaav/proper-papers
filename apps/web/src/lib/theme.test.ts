import assert from "node:assert/strict";
import { test } from "node:test";

import {
  THEME_COOKIE,
  effectiveTheme,
  otherTheme,
  parseTheme,
  themeCookie,
} from "./theme.ts";

test("only the two theme words are accepted from a cookie", () => {
  assert.equal(parseTheme("light"), "light");
  assert.equal(parseTheme("dark"), "dark");
  for (const bad of [
    "",
    "LIGHT",
    "Dark",
    "system",
    "light; path=/",
    "<b>",
    undefined,
    null,
  ]) {
    assert.equal(parseTheme(bad), null, String(bad));
  }
});

test("the toggle always goes to the other theme", () => {
  assert.equal(otherTheme("dark"), "light");
  assert.equal(otherTheme("light"), "dark");
});

test("a choice beats the system preference; with none, the system decides", () => {
  assert.equal(effectiveTheme("light", true), "light");
  assert.equal(effectiveTheme("dark", false), "dark");
  assert.equal(effectiveTheme(undefined, true), "dark");
  assert.equal(effectiveTheme(undefined, false), "light");
  // A garbled cookie is ignored, not trusted.
  assert.equal(effectiveTheme("evil", true), "dark");
  assert.equal(effectiveTheme("evil", false), "light");
});

test("the choice is remembered for a year, site-wide, and only over HTTPS when secure", () => {
  assert.equal(
    themeCookie("light", false),
    `${THEME_COOKIE}=light; Path=/; Max-Age=31536000; SameSite=Lax`,
  );
  assert.equal(
    themeCookie("dark", true),
    `${THEME_COOKIE}=dark; Path=/; Max-Age=31536000; SameSite=Lax; Secure`,
  );
  // The cookie carries the word and nothing else: no session, no identifiers.
  assert.doesNotMatch(themeCookie("dark", true), /token|session|id=/i);
});
