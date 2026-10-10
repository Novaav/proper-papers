import assert from "node:assert/strict";
import { test } from "node:test";

import type { ProcessingResult } from "../api/processing.ts";
import { handleStatus } from "./status-handler.ts";

const TENANT = "01a0e9bd-65a9-700c-a38b-c2210bcb6380";
const WORKSPACE = "01a0e9d1-6f65-7ac6-b4b3-84557cd2b17f";
const DOCUMENT = "01a0f000-0000-7000-8000-000000000001";
const SESSION = { accessToken: "tok", expiresAt: 2e9, createdAt: 1e9 };
const RUN = {
  id: "01a0f000-0000-7000-8000-00000000000a",
  status: "needs_review" as const,
  failureCode: null,
  attempts: 1,
  pipelineVersion: "p",
  createdAt: "2026-09-30T10:00:00Z",
  startedAt: "2026-09-30T10:00:01Z",
  finishedAt: "2026-09-30T10:00:03Z",
};
const RESULT = {
  outcome: "needs_review" as const,
  extractor: "rules-1",
  fields: {} as never,
};

async function status(
  result: ProcessingResult,
  session: typeof SESSION | null = SESSION,
  ids = [TENANT, WORKSPACE, DOCUMENT],
) {
  const calls: string[][] = [];
  const response = await handleStatus(ids[0]!, ids[1]!, ids[2]!, {
    readSession: async () => session,
    getProcessing: async (...args) => {
      calls.push(args);
      return result;
    },
    log: () => {},
  });
  return { response, body: await response.json(), calls };
}

test("a run answers only its state, never the result", async () => {
  const { response, body, calls } = await status({
    state: "ok",
    run: RUN,
    result: RESULT,
  });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.deepEqual(body, {
    state: "ok",
    status: "needs_review",
    attempts: 1,
    startedAt: RUN.startedAt,
  });
  assert.deepEqual(calls, [["tok", TENANT, WORKSPACE, DOCUMENT]]);
});

test("every other outcome maps to a fixed state", async () => {
  const cases: [ProcessingResult, number, string][] = [
    [{ state: "ok", run: null, result: null }, 200, "none"],
    [{ state: "not_scanned" }, 409, "not_clean"],
    [{ state: "blocked" }, 409, "not_clean"],
    [{ state: "not_found" }, 404, "not_found"],
    [{ state: "unauthorized" }, 401, "signed_out"],
    [{ state: "unavailable" }, 503, "unavailable"],
  ];
  for (const [result, code, state] of cases) {
    const { response, body } = await status(result);
    assert.deepEqual([response.status, body], [code, { state }], state);
  }
});

test("no session and malformed IDs never reach the API", async () => {
  const signedOut = await status({ state: "unavailable" }, null);
  assert.deepEqual(
    [signedOut.response.status, signedOut.body],
    [401, { state: "signed_out" }],
  );
  assert.equal(signedOut.calls.length, 0);
  const bad = await status({ state: "unavailable" }, SESSION, [
    TENANT,
    WORKSPACE,
    "x",
  ]);
  assert.deepEqual(
    [bad.response.status, bad.body],
    [404, { state: "not_found" }],
  );
  assert.equal(bad.calls.length, 0);
});
