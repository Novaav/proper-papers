import assert from "node:assert/strict";
import { test } from "node:test";

import { getProcessing, startProcessing } from "./processing.ts";

const BASE = "http://api.test";
const TENANT = "01a0e9bd-65a9-700c-a38b-c2210bcb6380";
const WORKSPACE = "01a0e9d1-6f65-7ac6-b4b3-84557cd2b17f";
const DOCUMENT = "01a0f000-0000-7000-8000-000000000001";
const RUN = "01a0f000-0000-7000-8000-00000000000a";
const PATH = `/v1/tenants/${TENANT}/workspaces/${WORKSPACE}/documents/${DOCUMENT}/processing`;

export const API_RUN = {
  id: RUN,
  status: "needs_review",
  failure_code: null,
  attempts: 1,
  pipeline_version:
    "service_agreement.v1+rules-1+parser@sha256:" + "a".repeat(64),
  created_at: "2026-09-30T10:00:00.000000+00:00",
  started_at: "2026-09-30T10:00:01.000000+00:00",
  finished_at: "2026-09-30T10:00:03.000000+00:00",
};
const unknown = { status: "unknown", reason: "not_found", candidates: [] };
function span(quote: string, page = 1) {
  return { page, start: 10, end: 10 + quote.length, quote };
}
export const API_RESULT = {
  schema: "service_agreement.v1",
  extractor: "rules-1",
  outcome: "needs_review",
  fields: {
    provider_name: {
      status: "found",
      reason: null,
      candidates: [
        {
          value: "Northwind Analytics Ltd",
          raw: "Northwind Analytics Ltd",
          evidence: [span("Provider: Northwind Analytics Ltd")],
        },
      ],
    },
    customer_name: unknown,
    effective_date: {
      status: "conflict",
      reason: null,
      candidates: [
        {
          value: "2026-03-01",
          raw: "1 March 2026",
          evidence: [span("Effective Date: 1 March 2026")],
        },
        {
          value: "2026-04-01",
          raw: "1 April 2026",
          evidence: [span("is effective as of 1 April 2026", 2)],
        },
      ],
    },
    end_date: unknown,
    governing_law: unknown,
    contract_value: {
      status: "found",
      reason: null,
      candidates: [
        {
          value: { amount: "120000.00", currency: "EUR" },
          raw: "EUR 120,000.00",
          evidence: [span("Contract Value: EUR 120,000.00")],
        },
      ],
    },
    notice_period_days: {
      status: "unknown",
      reason: "unparseable",
      candidates: [
        {
          value: null,
          raw: "three months",
          evidence: [span("Notice Period: three months")],
        },
      ],
    },
  },
};

function reply(status: number, body: unknown) {
  const calls: {
    url: string;
    method: string | undefined;
    auth: string | null;
  }[] = [];
  const fetchImpl: typeof fetch = async (input, init) => {
    calls.push({
      url: String(input),
      method: init?.method,
      auth: new Headers(init?.headers).get("authorization"),
    });
    return Response.json(body, { status });
  };
  return { calls, fetchImpl };
}

const get = (fetchImpl: typeof fetch) =>
  getProcessing(BASE, "token", TENANT, WORKSPACE, DOCUMENT, fetchImpl);
const start = (fetchImpl: typeof fetch) =>
  startProcessing(BASE, "token", TENANT, WORKSPACE, DOCUMENT, fetchImpl);

test("get: a finished run with its result, typed", async () => {
  const { calls, fetchImpl } = reply(200, { run: API_RUN, result: API_RESULT });
  const answer = await get(fetchImpl);
  assert.equal(calls[0]?.url, BASE + PATH);
  assert.equal(calls[0]?.auth, "Bearer token");
  assert.equal(answer.state, "ok");
  assert.ok(answer.state === "ok" && answer.run && answer.result);
  assert.equal(answer.run.status, "needs_review");
  assert.deepEqual(answer.result.fields.contract_value.candidates[0]?.value, {
    amount: "120000.00",
    currency: "EUR",
  });
  assert.deepEqual(
    answer.result.fields.effective_date.candidates[1]?.evidence,
    [{ page: 2, quote: "is effective as of 1 April 2026" }],
  );
});

test("get: never processed and still running carry no result", async () => {
  assert.deepEqual(
    await get(reply(200, { run: null, result: null }).fetchImpl),
    {
      state: "ok",
      run: null,
      result: null,
    },
  );
  const running = { ...API_RUN, status: "running", finished_at: null };
  const answer = await get(
    reply(200, { run: running, result: null }).fetchImpl,
  );
  assert.ok(
    answer.state === "ok" &&
      answer.run?.status === "running" &&
      answer.result === null,
  );
  const failed = {
    ...API_RUN,
    status: "failed",
    failure_code: "no_text_layer",
  };
  const failure = await get(
    reply(200, { run: failed, result: null }).fetchImpl,
  );
  assert.ok(
    failure.state === "ok" && failure.run?.failureCode === "no_text_layer",
  );
});

test("get: 409 not clean, 404, 401, errors and network failures", async () => {
  assert.deepEqual(await get(reply(409, { detail: "not_scanned" }).fetchImpl), {
    state: "not_scanned",
  });
  assert.deepEqual(await get(reply(409, { detail: "blocked" }).fetchImpl), {
    state: "blocked",
  });
  assert.deepEqual(await get(reply(404, { detail: "not_found" }).fetchImpl), {
    state: "not_found",
  });
  assert.deepEqual(await get(reply(401, {}).fetchImpl), {
    state: "unauthorized",
  });
  assert.deepEqual(await get(reply(503, {}).fetchImpl), {
    state: "unavailable",
  });
  const offline: typeof fetch = async () => {
    throw new TypeError("offline");
  };
  assert.deepEqual(await get(offline), { state: "unavailable" });
});

function mutate(path: (string | number)[], value: unknown): unknown {
  const copy = structuredClone(API_RESULT) as Record<string, unknown>;
  let target: Record<string | number, unknown> = copy;
  for (const key of path.slice(0, -1))
    target = target[key] as Record<string | number, unknown>;
  const last = path.at(-1) as string | number;
  if (value === undefined) delete target[last];
  else target[last] = value;
  return copy;
}

test("get: anything outside the contract makes the answer unavailable", async () => {
  const bad: unknown[] = [
    mutate(["schema"], "service_agreement.v2"),
    mutate(["outcome"], "completed"), // disagrees with the run
    mutate(["fields", "end_date"], undefined),
    mutate(["fields", "extra"], unknown),
    mutate(["fields", "provider_name", "status"], "maybe"),
    mutate(["fields", "customer_name", "reason"], null),
    mutate(["fields", "provider_name", "candidates"], []),
    mutate(["fields", "effective_date", "candidates", 1], undefined), // a one-candidate conflict
    mutate(
      ["fields", "effective_date", "candidates", 0, "value"],
      "1 March 2026",
    ),
    mutate(["fields", "contract_value", "candidates", 0, "value"], {
      amount: "1.5",
      currency: "EUR",
    }),
    mutate(["fields", "contract_value", "candidates", 0, "value"], 120000),
    mutate(["fields", "notice_period_days", "candidates", 0, "value"], "90"),
    mutate(["fields", "provider_name", "candidates", 0, "raw"], "Southwind"), // not inside its quote
    mutate(["fields", "provider_name", "candidates", 0, "evidence"], []),
    mutate(
      ["fields", "provider_name", "candidates", 0, "evidence", 0, "end"],
      99,
    ),
    mutate(
      ["fields", "provider_name", "candidates", 0, "evidence", 0, "page"],
      0,
    ),
  ];
  for (const result of bad) {
    assert.deepEqual(
      await get(reply(200, { run: API_RUN, result }).fetchImpl),
      { state: "unavailable" },
    );
  }
  for (const body of [
    null,
    [],
    { run: { ...API_RUN, status: "paused" }, result: null },
    { run: { ...API_RUN, status: "failed", failure_code: null }, result: null },
    { run: { ...API_RUN, id: "not-a-uuid" }, result: API_RESULT },
    { run: { ...API_RUN, status: "queued" }, result: API_RESULT },
    { run: null, result: API_RESULT },
  ]) {
    assert.deepEqual(
      await get(reply(200, body).fetchImpl),
      { state: "unavailable" },
      JSON.stringify(body),
    );
  }
});

test("start: 202 created, 200 existing, and every refusal", async () => {
  const queued = {
    ...API_RUN,
    status: "queued",
    attempts: 0,
    started_at: null,
    finished_at: null,
  };
  const created = reply(202, { run: queued });
  const answer = await start(created.fetchImpl);
  assert.equal(created.calls[0]?.method, "POST");
  assert.equal(created.calls[0]?.url, BASE + PATH);
  assert.ok(
    answer.state === "started" &&
      answer.created &&
      answer.run.status === "queued",
  );
  const existing = await start(reply(200, { run: API_RUN }).fetchImpl);
  assert.ok(existing.state === "started" && !existing.created);
  assert.deepEqual(
    await start(reply(409, { detail: "not_scanned" }).fetchImpl),
    { state: "not_scanned" },
  );
  assert.deepEqual(await start(reply(409, { detail: "blocked" }).fetchImpl), {
    state: "blocked",
  });
  assert.deepEqual(
    await start(reply(503, { detail: "processing_unavailable" }).fetchImpl),
    {
      state: "processing_unavailable",
    },
  );
  assert.deepEqual(
    await start(reply(503, { detail: "unavailable" }).fetchImpl),
    { state: "unavailable" },
  );
  assert.deepEqual(await start(reply(404, {}).fetchImpl), {
    state: "not_found",
  });
  assert.deepEqual(await start(reply(401, {}).fetchImpl), {
    state: "unauthorized",
  });
  assert.deepEqual(await start(reply(202, { run: { id: "x" } }).fetchImpl), {
    state: "unavailable",
  });
});

test("malformed IDs never reach a URL", async () => {
  const { calls, fetchImpl } = reply(200, {});
  const cases: [string, string, string][] = [
    ["x", WORKSPACE, DOCUMENT],
    [TENANT, WORKSPACE, "../content"],
    [TENANT, WORKSPACE.toUpperCase(), DOCUMENT],
  ];
  for (const [tenant, workspace, document] of cases) {
    assert.deepEqual(
      await getProcessing(BASE, "t", tenant, workspace, document, fetchImpl),
      { state: "not_found" },
    );
    assert.deepEqual(
      await startProcessing(BASE, "t", tenant, workspace, document, fetchImpl),
      { state: "not_found" },
    );
  }
  assert.equal(calls.length, 0);
});
