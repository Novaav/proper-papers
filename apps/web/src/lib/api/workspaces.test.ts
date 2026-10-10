import assert from "node:assert/strict";
import { test } from "node:test";

import { createWorkspace, listWorkspaces } from "./workspaces.ts";

const BASE = "http://api.test";
const TENANT = "01a0e9bd-65a9-700c-a38b-c2210bcb6380";
const WORKSPACE = {
  id: "01a0e9d1-6f65-7ac6-b4b3-84557cd2b17f",
  name: "Contracts",
  created_at: "2026-09-28T21:00:12.003958+00:00",
};

interface Seen {
  url: string;
  method: string | undefined;
  auth: string | null;
  body: unknown;
  redirect: RequestRedirect | undefined;
}

function recorder(response: () => Response) {
  const calls: Seen[] = [];
  const fetchImpl: typeof fetch = async (input, init) => {
    calls.push({
      url: String(input),
      method: init?.method,
      auth: new Headers(init?.headers).get("authorization"),
      body:
        init?.body === undefined ? undefined : JSON.parse(String(init.body)),
      redirect: init?.redirect,
    });
    return response();
  };
  return { calls, fetchImpl };
}

test("list: GET with the bearer token only, parses workspaces", async () => {
  const { calls, fetchImpl } = recorder(() =>
    Response.json({ workspaces: [WORKSPACE] }),
  );
  const result = await listWorkspaces(BASE, "tok", TENANT, fetchImpl);
  assert.deepEqual(calls, [
    {
      url: `${BASE}/v1/tenants/${TENANT}/workspaces`,
      method: "GET",
      auth: "Bearer tok",
      body: undefined,
      redirect: "error",
    },
  ]);
  assert.deepEqual(result, {
    state: "ok",
    workspaces: [
      { id: WORKSPACE.id, name: "Contracts", createdAt: WORKSPACE.created_at },
    ],
  });
});

test("create: POST JSON {name} and parse the created workspace", async () => {
  const { calls, fetchImpl } = recorder(() =>
    Response.json(WORKSPACE, { status: 201 }),
  );
  const result = await createWorkspace(
    BASE,
    "tok",
    TENANT,
    "  padded  ",
    fetchImpl,
  );
  assert.equal(calls[0]?.method, "POST");
  assert.deepEqual(calls[0]?.body, { name: "  padded  " });
  assert.equal(result.state, "created");
});

test("a non-canonical tenant ID never reaches a URL", async () => {
  for (const tenant of [
    "../../v1/me",
    "not-a-uuid",
    TENANT.toUpperCase(),
    `${TENANT}/x`,
  ]) {
    const { calls, fetchImpl } = recorder(() =>
      Response.json({ workspaces: [] }),
    );
    assert.deepEqual(await listWorkspaces(BASE, "t", tenant, fetchImpl), {
      state: "not_found",
    });
    assert.deepEqual(await createWorkspace(BASE, "t", tenant, "x", fetchImpl), {
      state: "not_found",
    });
    assert.equal(calls.length, 0, tenant);
  }
});

test("status mapping for list", async () => {
  for (const [status, state] of [
    [404, "not_found"],
    [401, "unauthorized"],
    [503, "unavailable"],
    [500, "unavailable"],
    [302, "unavailable"],
  ] as const) {
    const { fetchImpl } = recorder(() => new Response(null, { status }));
    assert.deepEqual(
      await listWorkspaces(BASE, "t", TENANT, fetchImpl),
      { state },
      String(status),
    );
  }
});

test("status mapping for create", async () => {
  for (const [status, state] of [
    [422, "invalid_name"],
    [404, "not_found"],
    [401, "unauthorized"],
    [503, "unavailable"],
    [200, "unavailable"],
  ] as const) {
    const { fetchImpl } = recorder(() => Response.json({}, { status }));
    assert.deepEqual(
      await createWorkspace(BASE, "t", TENANT, "x", fetchImpl),
      { state },
      String(status),
    );
  }
});

test("malformed bodies, network errors and missing configuration are unavailable", async () => {
  for (const body of [
    "not json",
    JSON.stringify({}),
    JSON.stringify({ workspaces: {} }),
    JSON.stringify({ workspaces: [{ ...WORKSPACE, id: "x" }] }),
    JSON.stringify({ workspaces: [{ ...WORKSPACE, name: 1 }] }),
    JSON.stringify({ workspaces: [{ ...WORKSPACE, created_at: "yesterday" }] }),
  ]) {
    const { fetchImpl } = recorder(() => new Response(body, { status: 200 }));
    assert.deepEqual(
      await listWorkspaces(BASE, "t", TENANT, fetchImpl),
      { state: "unavailable" },
      body,
    );
  }
  const failing: typeof fetch = async () => {
    throw new Error("ECONNREFUSED");
  };
  assert.deepEqual(await listWorkspaces(BASE, "t", TENANT, failing), {
    state: "unavailable",
  });
  assert.deepEqual(await createWorkspace(BASE, "t", TENANT, "x", failing), {
    state: "unavailable",
  });
  assert.deepEqual(await listWorkspaces(undefined, "t", TENANT, failing), {
    state: "unavailable",
  });
});
