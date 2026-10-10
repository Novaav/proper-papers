import assert from "node:assert/strict";
import { test } from "node:test";

import { getMe } from "./me.ts";

const BASE = "http://api.test";

test("sends the token only as a bearer header to /v1/me and parses the body", async () => {
  let seen:
    | {
        url: string;
        auth: string | null;
        redirect: RequestRedirect | undefined;
      }
    | undefined;
  const fetchImpl: typeof fetch = async (input, init) => {
    seen = {
      url: String(input),
      auth: new Headers(init?.headers).get("authorization"),
      redirect: init?.redirect,
    };
    return Response.json({
      user_id: "u1",
      memberships: [{ tenant_id: "t1", role: "owner" }],
    });
  };
  const result = await getMe(BASE, "tok", fetchImpl);
  assert.deepEqual(seen, {
    url: "http://api.test/v1/me",
    auth: "Bearer tok",
    redirect: "error",
  });
  assert.deepEqual(result, {
    state: "ok",
    me: { userId: "u1", memberships: [{ tenantId: "t1", role: "owner" }] },
  });
});

test("401 is reported as unauthorized; other failures as unavailable without details", async () => {
  const respond =
    (response: Response): typeof fetch =>
    async () =>
      response;
  assert.deepEqual(
    await getMe(BASE, "t", respond(new Response(null, { status: 401 }))),
    {
      state: "unauthorized",
    },
  );
  assert.deepEqual(
    await getMe(BASE, "t", respond(new Response(null, { status: 503 }))),
    {
      state: "unavailable",
      reason: "HTTP 503",
    },
  );
  assert.deepEqual(await getMe(BASE, "t", respond(new Response("x"))), {
    state: "unavailable",
    reason: "response was not JSON",
  });
  for (const body of [
    {},
    { user_id: 1, memberships: [] },
    { user_id: "u", memberships: [{ tenant_id: "t", role: "admin" }] },
  ]) {
    assert.equal(
      (await getMe(BASE, "t", respond(Response.json(body)))).state,
      "unavailable",
    );
  }
  const failing: typeof fetch = async () => {
    throw new Error("connect ECONNREFUSED with secret details");
  };
  assert.deepEqual(await getMe(BASE, "t", failing), {
    state: "unavailable",
    reason: "API unreachable",
  });
  assert.deepEqual(await getMe(undefined, "t", failing), {
    state: "unavailable",
    reason: "API_BASE_URL is not set",
  });
});

test("every membership role the API has is accepted (owner, reviewer, member)", async () => {
  const roles = ["owner", "reviewer", "member"] as const;
  const fetchImpl: typeof fetch = async () =>
    Response.json({
      user_id: "u1",
      memberships: roles.map((role, i) => ({ tenant_id: `t${i}`, role })),
    });
  assert.deepEqual(await getMe(BASE, "tok", fetchImpl), {
    state: "ok",
    me: {
      userId: "u1",
      memberships: roles.map((role, i) => ({ tenantId: `t${i}`, role })),
    },
  });
});
