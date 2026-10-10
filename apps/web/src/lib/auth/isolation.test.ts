// Static guards for token isolation and cookie handling.

import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { test } from "node:test";

const SRC = join(import.meta.dirname, "..", "..");

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.(ts|tsx)$/.test(name) &&
      !name.endsWith(".test.ts") &&
      !name.includes(".testsupport.")
      ? [path]
      : [];
  });
}

const files = sourceFiles(SRC).map((path) => ({
  path: relative(SRC, path),
  text: readFileSync(path, "utf8"),
}));

test("no client component imports authentication, API or document handler modules", () => {
  const offenders = files.filter(
    (f) =>
      /^\s*["']use client["']/m.test(f.text) &&
      /from ["'][^"']*lib\/(auth|api|documents)\//.test(f.text),
  );
  assert.deepEqual(
    offenders.map((f) => f.path),
    [],
  );
});

test("the server entry point is marked server-only", () => {
  const server = files.find((f) => f.path === join("lib", "auth", "server.ts"));
  assert.ok(server?.text.includes('import "server-only";'));
});

test("only route handlers change cookies; pages and server code only read them", () => {
  const writers = files
    .filter((f) => /cookies\.(set|delete)\(/.test(f.text))
    .map((f) => f.path);
  assert.deepEqual(writers, [join("lib", "auth", "handlers.ts")]);
  const handlerUsers = files
    .filter((f) => f.text.includes("@/lib/auth/handlers"))
    .map((f) => f.path)
    .sort();
  assert.deepEqual(handlerUsers, [
    join("app", "auth", "callback", "route.ts"),
    join("app", "auth", "login", "route.ts"),
    join("app", "auth", "logout", "route.ts"),
  ]);
});

test("the test-only fake IdP is never imported by application code", () => {
  assert.deepEqual(
    files.filter((f) => f.text.includes("fake-idp")).map((f) => f.path),
    [],
  );
});

test("tokens and secrets are never logged", () => {
  const logging = files.filter((f) =>
    /console\.(log|info|warn|error)\([^)]*(accessToken|access_token|clientSecret|SESSION_SECRET|code_verifier|codeVerifier)/.test(
      f.text,
    ),
  );
  assert.deepEqual(
    logging.map((f) => f.path),
    [],
  );
});

test("the proxy sets headers only: no authentication or API code", () => {
  const proxy = files.find((f) => f.path === "proxy.ts");
  assert.ok(proxy, "src/proxy.ts exists");
  assert.equal(/lib\/(auth|api|workspaces)\//.test(proxy.text), false);
  assert.equal(
    /cookies|session/i.test(proxy.text.replace(/\/\/.*$/gm, "")),
    false,
  );
});

test("app error pages are CSP-safe and reveal no error details", () => {
  // Next.js's built-in 404 renders inline styles that the Content-Security-
  // Policy blocks; its built-in error UI uses inline style props too. The
  // app's own pages use none.
  const pages = [
    "app/error.tsx",
    "app/global-error.tsx",
    "app/not-found.tsx",
    "app/tenants/[tenantId]/workspaces/error.tsx",
    "app/tenants/[tenantId]/workspaces/not-found.tsx",
    "app/tenants/[tenantId]/workspaces/[workspaceId]/not-found.tsx",
  ];
  for (const page of pages) {
    const file = files.find((f) => f.path === join(...page.split("/")));
    assert.ok(file, `${page} exists`);
    // Code only: comments may explain why inline styles are avoided.
    const code = file.text.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");
    assert.equal(
      /\sstyle=|<style|dangerouslySetInnerHTML/.test(code),
      false,
      page,
    );
    assert.equal(/error\.(message|stack|digest)/.test(code), false, page);
  }
  for (const page of ["app/error.tsx", "app/global-error.tsx"]) {
    const text =
      files.find((f) => f.path === join(...page.split("/")))?.text ?? "";
    assert.match(text, /^"use client";/, page);
  }
  const globalError =
    files.find((f) => f.path === join("app", "global-error.tsx"))?.text ?? "";
  assert.match(globalError, /<html lang="en">[\s\S]*<body>/);
  assert.match(globalError, /import "\.\/styles\/index\.css";/);
});
