import assert from "node:assert/strict";
import { test } from "node:test";

import type { CreateResult } from "../api/workspaces.ts";
import type { Session } from "../auth/session.ts";
import {
  MAX_FORM_BYTES,
  handleCreateWorkspace,
  isValidName,
  type CreateDeps,
} from "./create-handler.ts";

const ORIGIN = "https://app.test";
const TENANT = "01a0e9bd-65a9-700c-a38b-c2210bcb6380";
const LIST = `${ORIGIN}/tenants/${TENANT}/workspaces`;
const LOGIN = `${ORIGIN}/auth/login?returnTo=${encodeURIComponent(`/tenants/${TENANT}/workspaces`)}`;
const NOW = 1_800_000_000;
const OLD_SESSION: Session = {
  accessToken: "tok",
  expiresAt: NOW + 600,
  createdAt: NOW - 120,
};

function setup(
  result: CreateResult = {
    state: "created",
    workspace: { id: TENANT, name: "x", createdAt: "" },
  },
  session: Session | null = OLD_SESSION,
) {
  const calls: { token: string; tenant: string; name: string }[] = [];
  const logs: string[] = [];
  const deps: CreateDeps = {
    appOrigin: ORIGIN,
    readSession: async () => session,
    createWorkspace: async (token, tenant, name) => {
      calls.push({ token, tenant, name });
      return result;
    },
    now: () => NOW,
    log: (message) => logs.push(message),
  };
  return { calls, logs, deps };
}

function post(body: string, headers: Record<string, string> = {}): Request {
  return new Request(`${ORIGIN}/tenants/${TENANT}/workspaces/create`, {
    method: "POST",
    headers: {
      Origin: ORIGIN,
      "Content-Type": "application/x-www-form-urlencoded",
      ...headers,
    },
    body,
  });
}

function assertRedirect(response: Response, location: string) {
  assert.equal(response.status, 303);
  assert.equal(response.headers.get("location"), location);
  assert.equal(response.headers.get("cache-control"), "no-store");
}

test("a valid form creates the workspace through the API and redirects with a fixed code", async () => {
  const { calls, deps } = setup();
  const response = await handleCreateWorkspace(
    post("name=%20Contracts%20"),
    TENANT,
    deps,
  );
  assertRedirect(response, `${LIST}?created=1`);
  assert.deepEqual(calls, [
    { token: "tok", tenant: TENANT, name: " Contracts " },
  ]);
});

for (const origin of [
  null,
  "https://evil.test",
  "null",
  `${ORIGIN}/`,
  "http://app.test",
]) {
  test(`Origin ${String(origin)} is refused before anything else`, async () => {
    const { calls, logs, deps } = setup();
    const headers: Record<string, string> =
      origin === null ? {} : { Origin: origin };
    const request = new Request(
      `${ORIGIN}/tenants/${TENANT}/workspaces/create`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          ...headers,
        },
        body: "name=x",
      },
    );
    const response = await handleCreateWorkspace(request, TENANT, deps);
    assert.equal(response.status, 403);
    assert.equal(calls.length, 0);
    assert.deepEqual(logs, ["create workspace refused: origin_mismatch"]);
  });
}

test("without configuration the route is unavailable", async () => {
  const { calls, deps } = setup();
  const response = await handleCreateWorkspace(post("name=x"), TENANT, {
    ...deps,
    appOrigin: null,
  });
  assert.equal(response.status, 503);
  assert.equal(calls.length, 0);
});

for (const tenant of [
  "not-a-uuid",
  TENANT.toUpperCase(),
  "..",
  `${TENANT}%2F..`,
]) {
  test(`malformed tenant ${tenant} is 404 without an API call`, async () => {
    const { calls, deps } = setup();
    const response = await handleCreateWorkspace(post("name=x"), tenant, deps);
    assert.equal(response.status, 404);
    assert.equal(calls.length, 0);
  });
}

test("without a session the user is sent to login, returning to the list", async () => {
  const { calls, deps } = setup(undefined, null);
  assertRedirect(
    await handleCreateWorkspace(post("name=x"), TENANT, deps),
    LOGIN,
  );
  assert.equal(calls.length, 0);
});

for (const [label, body] of [
  ["empty", "name="],
  ["spaces only", "name=%20%20%20"],
  ["too long", `name=${"x".repeat(201)}`],
  ["NUL", "name=a%00b"],
  ["missing", "other=x"],
  ["repeated", "name=a&name=b"],
  ["oversized body", `name=${"x".repeat(MAX_FORM_BYTES)}`],
] as const) {
  test(`invalid name (${label}) redirects with the fixed code and no API call`, async () => {
    const { calls, deps } = setup();
    assertRedirect(
      await handleCreateWorkspace(post(body), TENANT, deps),
      `${LIST}?error=invalid_name`,
    );
    assert.equal(calls.length, 0);
  });
}

test("a non-form body is a bad request", async () => {
  const { calls, deps } = setup();
  for (const type of [
    "application/json",
    "multipart/form-data; boundary=x",
    "text/plain",
  ]) {
    const response = await handleCreateWorkspace(
      post("name=x", { "Content-Type": type }),
      TENANT,
      deps,
    );
    assert.equal(response.status, 400, type);
  }
  assert.equal(calls.length, 0);
});

test("API outcomes map to fixed redirects", async () => {
  const cases: [CreateResult, string][] = [
    [{ state: "invalid_name" }, `${LIST}?error=invalid_name`],
    [{ state: "not_found" }, LIST],
    [{ state: "unavailable" }, `${LIST}?error=unavailable`],
    [{ state: "unauthorized" }, LOGIN],
  ];
  for (const [result, location] of cases) {
    const { deps } = setup(result);
    assertRedirect(
      await handleCreateWorkspace(post("name=x"), TENANT, deps),
      location,
    );
  }
});

test("a token rejected right after login shows an error instead of looping", async () => {
  const fresh = { ...OLD_SESSION, createdAt: NOW - 5 };
  const { deps } = setup({ state: "unauthorized" }, fresh);
  assertRedirect(
    await handleCreateWorkspace(post("name=x"), TENANT, deps),
    `${LIST}?error=rejected`,
  );
});

test("responses never echo the submitted name", async () => {
  const { deps } = setup({ state: "invalid_name" });
  const response = await handleCreateWorkspace(
    post("name=%3Cscript%3Ealert(1)%3C%2Fscript%3E"),
    TENANT,
    deps,
  );
  assert.ok(!(response.headers.get("location") ?? "").includes("script"));
  assert.equal(await response.text(), "");
});

test("name rule mirrors the API (spaces trimmed, code points counted)", () => {
  assert.equal(isValidName("x"), true);
  assert.equal(isValidName(` ${"x".repeat(200)} `), true);
  assert.equal(isValidName("\u{1F600}".repeat(200)), true);
  assert.equal(isValidName("\u{1F600}".repeat(201)), false);
  assert.equal(isValidName("\t"), true); // the API trims spaces only
  assert.equal(isValidName(" "), false);
});
