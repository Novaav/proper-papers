import assert from "node:assert/strict";
import { test } from "node:test";

import type { StartResult } from "../api/processing.ts";
import type { Session } from "../auth/session.ts";
import { handleProcess, type ProcessDeps } from "./process-handler.ts";

const ORIGIN = "https://app.test";
const TENANT = "01a0e9bd-65a9-700c-a38b-c2210bcb6380";
const WORKSPACE = "01a0e9d1-6f65-7ac6-b4b3-84557cd2b17f";
const DOCUMENT = "01a0f000-0000-7000-8000-000000000001";
const PAGE = `/tenants/${TENANT}/workspaces/${WORKSPACE}/documents/${DOCUMENT}`;
const NOW = 1_800_000_000;
const OLD_SESSION: Session = {
  accessToken: "tok",
  expiresAt: NOW + 600,
  createdAt: NOW - 120,
};
const RUN = {
  id: "01a0f000-0000-7000-8000-00000000000a",
  status: "queued" as const,
  failureCode: null,
  attempts: 0,
  pipelineVersion: "p",
  createdAt: "2026-09-30T10:00:00Z",
  startedAt: null,
  finishedAt: null,
};

function setup(result: StartResult, session: Session | null = OLD_SESSION) {
  const calls: string[][] = [];
  const logs: string[] = [];
  const deps: ProcessDeps = {
    appOrigin: ORIGIN,
    readSession: async () => session,
    start: async (...args) => {
      calls.push(args);
      return result;
    },
    now: () => NOW,
    log: (message) => logs.push(message),
  };
  return { calls, logs, deps };
}

function post(origin: string | null = ORIGIN): Request {
  return new Request(`${ORIGIN}${PAGE}/process`, {
    method: "POST",
    headers: origin === null ? {} : { Origin: origin },
  });
}

const run = (
  result: StartResult,
  request = post(),
  ids = [TENANT, WORKSPACE, DOCUMENT],
) => {
  const context = setup(result);
  return {
    context,
    response: handleProcess(request, ids[0]!, ids[1]!, ids[2]!, context.deps),
  };
};

test("a new run redirects to the document page with started; an existing one without a code", async () => {
  const created = run({ state: "started", created: true, run: RUN });
  const response = await created.response;
  assert.equal(response.status, 303);
  assert.equal(
    response.headers.get("location"),
    `${ORIGIN}${PAGE}?processing=started`,
  );
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.deepEqual(created.context.calls, [
    ["tok", TENANT, WORKSPACE, DOCUMENT],
  ]);
  const existing = await run({ state: "started", created: false, run: RUN })
    .response;
  assert.equal(existing.headers.get("location"), `${ORIGIN}${PAGE}`);
});

test("every refusal becomes a fixed code on the document page", async () => {
  const cases: [StartResult, string][] = [
    [{ state: "not_scanned" }, "?processing=not_scanned"],
    [{ state: "blocked" }, "?processing=blocked"],
    [{ state: "processing_unavailable" }, "?processing=processing_unavailable"],
    [{ state: "unavailable" }, "?processing=unavailable"],
    [{ state: "not_found" }, ""],
  ];
  for (const [result, query] of cases) {
    const response = await run(result).response;
    assert.equal(
      response.headers.get("location"),
      `${ORIGIN}${PAGE}${query}`,
      result.state,
    );
  }
});

test("a foreign or missing Origin is refused before anything else", async () => {
  for (const origin of [null, "https://evil.test", `${ORIGIN}.evil.test`]) {
    const { context, response } = run({ state: "unavailable" }, post(origin));
    assert.equal((await response).status, 403);
    assert.equal(context.calls.length, 0);
    assert.deepEqual(context.logs, [
      "process document refused: origin_mismatch",
    ]);
  }
});

test("malformed IDs are 404 without an API call; no session goes to sign-in", async () => {
  const bad = run({ state: "unavailable" }, post(), [
    TENANT,
    WORKSPACE,
    "../x",
  ]);
  assert.equal((await bad.response).status, 404);
  assert.equal(bad.context.calls.length, 0);
  const anonymous = setup({ state: "unavailable" }, null);
  const response = await handleProcess(
    post(),
    TENANT,
    WORKSPACE,
    DOCUMENT,
    anonymous.deps,
  );
  assert.equal(
    response.headers.get("location"),
    `${ORIGIN}/auth/login?returnTo=${encodeURIComponent(PAGE)}`,
  );
  assert.equal(anonymous.calls.length, 0);
});

test("a rejected token: an older session signs in again, a fresh one sees rejected", async () => {
  const old = await run({ state: "unauthorized" }).response;
  assert.equal(
    old.headers.get("location"),
    `${ORIGIN}/auth/login?returnTo=${encodeURIComponent(PAGE)}`,
  );
  const fresh = setup(
    { state: "unauthorized" },
    { ...OLD_SESSION, createdAt: NOW - 1 },
  );
  const response = await handleProcess(
    post(),
    TENANT,
    WORKSPACE,
    DOCUMENT,
    fresh.deps,
  );
  assert.equal(
    response.headers.get("location"),
    `${ORIGIN}${PAGE}?processing=rejected`,
  );
});

test("without configuration the route is unavailable", async () => {
  const { deps } = setup({ state: "unavailable" });
  const response = await handleProcess(post(), TENANT, WORKSPACE, DOCUMENT, {
    ...deps,
    appOrigin: null,
  });
  assert.equal(response.status, 503);
});
