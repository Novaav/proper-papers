import assert from "node:assert/strict";
import { test } from "node:test";

import {
  exportApproval,
  getReview,
  postApproval,
  postDecision,
} from "./review.ts";

const BASE = "http://api.test";
const TENANT = "01a0e9bd-65a9-700c-a38b-c2210bcb6380";
const WORKSPACE = "01a0e9d1-6f65-7ac6-b4b3-84557cd2b17f";
const DOCUMENT = "01a0f000-0000-7000-8000-000000000001";
const RUN = "01a0f000-0000-7000-8000-00000000000a";
const USER = "01a0f000-0000-7000-8000-0000000000aa";
const PATH = `/v1/tenants/${TENANT}/workspaces/${WORKSPACE}/documents/${DOCUMENT}/review`;
const AT = "2026-10-01T10:00:00.000000Z";

const DECISION = {
  seq: 1,
  field_key: "effective_date",
  resolution: "candidate",
  candidate_ordinal: 1,
  decided_by: USER,
  decided_at: AT,
};
const APPROVAL = {
  version: 1,
  run_id: RUN,
  decisions_through: 1,
  approved_by: USER,
  approved_at: AT,
  record_sha256: "a".repeat(64),
};
export const API_REVIEW = {
  run_id: RUN,
  can_review: true,
  decisions_through: 1,
  decisions: [DECISION],
  approvals: [APPROVAL],
};

function respond(response: Response) {
  const seen: {
    url: string;
    method: string;
    body: string | null;
    type: string | null;
  }[] = [];
  const fetchImpl: typeof fetch = async (input, init) => {
    seen.push({
      url: String(input),
      method: init?.method ?? "GET",
      body: init?.body
        ? new TextDecoder().decode(init.body as Uint8Array)
        : null,
      type: new Headers(init?.headers).get("content-type"),
    });
    return response.clone();
  };
  return { seen, fetchImpl };
}

const ids = [TENANT, WORKSPACE, DOCUMENT] as const;
const error = (status: number, detail: unknown) =>
  Response.json({ detail }, { status });

test("the review state is parsed strictly", async () => {
  const { seen, fetchImpl } = respond(Response.json(API_REVIEW));
  assert.deepEqual(await getReview(BASE, "tok", ...ids, fetchImpl), {
    state: "ok",
    review: {
      runId: RUN,
      canReview: true,
      decisionsThrough: 1,
      decisions: [
        {
          seq: 1,
          fieldKey: "effective_date",
          resolution: "candidate",
          candidateOrdinal: 1,
          decidedAt: AT,
        },
      ],
      approvals: [
        {
          version: 1,
          runId: RUN,
          decisionsThrough: 1,
          approvedAt: AT,
          recordSha256: "a".repeat(64),
        },
      ],
    },
  });
  assert.equal(seen[0]?.url, `${BASE}${PATH}`);
  const empty = {
    ...API_REVIEW,
    run_id: null,
    decisions_through: 0,
    decisions: [],
    approvals: [],
  };
  assert.equal(
    (
      await getReview(
        BASE,
        "t",
        ...ids,
        respond(Response.json(empty)).fetchImpl,
      )
    ).state,
    "ok",
  );
});

test("an unexpected review state is unavailable, never guessed", async () => {
  const bad = [
    { ...API_REVIEW, can_review: "yes" },
    { ...API_REVIEW, run_id: "not-a-uuid" },
    { ...API_REVIEW, decisions_through: 2 }, // does not match the decisions
    { ...API_REVIEW, decisions: [{ ...DECISION, seq: 2 }] }, // a gap
    { ...API_REVIEW, decisions: [{ ...DECISION, field_key: "notes" }] },
    { ...API_REVIEW, decisions: [{ ...DECISION, candidate_ordinal: null }] },
    { ...API_REVIEW, decisions: [{ ...DECISION, resolution: "unknown" }] }, // with an ordinal
    { ...API_REVIEW, decisions: [{ ...DECISION, decided_at: "yesterday" }] },
    { ...API_REVIEW, approvals: [{ ...APPROVAL, version: 2 }] },
    { ...API_REVIEW, approvals: [{ ...APPROVAL, record_sha256: "xyz" }] },
    [],
    null,
  ];
  for (const body of bad) {
    const { fetchImpl } = respond(Response.json(body));
    assert.deepEqual(
      await getReview(BASE, "t", ...ids, fetchImpl),
      { state: "unavailable" },
      JSON.stringify(body),
    );
  }
});

test("review reads: not clean, not found, signed out, malformed ids", async () => {
  const cases: [Response, string][] = [
    [error(409, "blocked"), "blocked"],
    [error(409, "not_scanned"), "not_scanned"],
    [error(404, "not_found"), "not_found"],
    [error(401, "unauthorized"), "unauthorized"],
    [error(503, "unavailable"), "unavailable"],
  ];
  for (const [response, state] of cases) {
    assert.equal(
      (await getReview(BASE, "t", ...ids, respond(response).fetchImpl)).state,
      state,
    );
  }
  const { seen, fetchImpl } = respond(Response.json(API_REVIEW));
  assert.equal(
    (await getReview(BASE, "t", TENANT, WORKSPACE, "../x", fetchImpl)).state,
    "not_found",
  );
  assert.equal(seen.length, 0);
});

test("a decision is sent as JSON with only the validated fields", async () => {
  const { seen, fetchImpl } = respond(Response.json(DECISION, { status: 201 }));
  const input = {
    runId: RUN,
    expectedSeq: 0,
    fieldKey: "effective_date" as const,
    resolution: "candidate" as const,
    candidateOrdinal: 1,
  };
  assert.deepEqual(await postDecision(BASE, "t", ...ids, input, fetchImpl), {
    state: "decided",
  });
  assert.deepEqual(seen[0], {
    url: `${BASE}${PATH}/decisions`,
    method: "POST",
    type: "application/json",
    body: JSON.stringify({
      run_id: RUN,
      expected_seq: 0,
      field_key: "effective_date",
      resolution: "candidate",
      candidate_ordinal: 1,
    }),
  });
  const unknown = respond(Response.json(DECISION, { status: 201 }));
  await postDecision(
    BASE,
    "t",
    ...ids,
    { ...input, resolution: "unknown", candidateOrdinal: null },
    unknown.fetchImpl,
  );
  assert.equal(
    JSON.parse(unknown.seen[0]?.body ?? "{}").candidate_ordinal,
    undefined,
  );
});

test("every refusal of a write is its own state", async () => {
  const input = {
    runId: RUN,
    expectedSeq: 0,
    fieldKey: "provider_name" as const,
    resolution: "unknown" as const,
    candidateOrdinal: null,
  };
  const cases: [Response, string][] = [
    [error(403, "not_reviewer"), "not_reviewer"],
    [error(409, "stale"), "stale"],
    [error(409, "run_changed"), "run_changed"],
    [error(409, "blocked"), "blocked"],
    [error(409, "not_scanned"), "not_scanned"],
    [error(422, "invalid_decision"), "invalid"],
    [error(422, "invalid_request"), "invalid"],
    [error(404, "not_found"), "not_found"],
    [error(401, "unauthorized"), "unauthorized"],
    [error(403, "something_else"), "unavailable"],
    [error(409, "something_else"), "unavailable"],
    [error(500, "boom"), "unavailable"],
    [Response.json({}, { status: 200 }), "unavailable"],
  ];
  for (const [response, state] of cases) {
    const result = await postDecision(
      BASE,
      "t",
      ...ids,
      input,
      respond(response).fetchImpl,
    );
    assert.equal(result.state, state, String(response.status));
  }
});

test("an approval reports the new version, or the unresolved fields", async () => {
  const input = { runId: RUN, expectedSeq: 1, expectedVersion: 0 };
  const created = respond(Response.json(APPROVAL, { status: 201 }));
  assert.deepEqual(
    await postApproval(BASE, "t", ...ids, input, created.fetchImpl),
    {
      state: "approved",
      version: 1,
    },
  );
  assert.deepEqual(JSON.parse(created.seen[0]?.body ?? "{}"), {
    run_id: RUN,
    expected_seq: 1,
    expected_version: 0,
  });
  const unresolved = Response.json(
    {
      detail: "unresolved_fields",
      fields: ["effective_date", "customer_name"],
    },
    { status: 422 },
  );
  assert.deepEqual(
    await postApproval(BASE, "t", ...ids, input, respond(unresolved).fetchImpl),
    {
      state: "unresolved",
      fields: ["effective_date", "customer_name"],
    },
  );
  const odd = Response.json(
    { detail: "unresolved_fields", fields: ["notes"] },
    { status: 422 },
  );
  assert.equal(
    (await postApproval(BASE, "t", ...ids, input, respond(odd).fetchImpl))
      .state,
    "unavailable",
  );
  assert.equal(
    (
      await postApproval(
        BASE,
        "t",
        ...ids,
        input,
        respond(error(409, "stale")).fetchImpl,
      )
    ).state,
    "stale",
  );
  const noVersion = Response.json({ ...APPROVAL, version: 0 }, { status: 201 });
  assert.equal(
    (await postApproval(BASE, "t", ...ids, input, respond(noVersion).fetchImpl))
      .state,
    "unavailable",
  );
});

test("the export passes the API's response through, or a fixed state", async () => {
  const body = Response.json({ schema: "approved_record.v1" });
  const { seen, fetchImpl } = respond(body);
  const ok = await exportApproval(BASE, "t", ...ids, 2, fetchImpl);
  assert.equal(ok.state, "ok");
  assert.equal(seen[0]?.url, `${BASE}${PATH}/approvals/2/export`);
  for (const [response, state] of [
    [error(404, "not_found"), "not_found"],
    [error(409, "blocked"), "blocked"],
    [error(401, "unauthorized"), "unauthorized"],
    [error(503, "unavailable"), "unavailable"],
  ] as const) {
    assert.equal(
      (await exportApproval(BASE, "t", ...ids, 1, respond(response).fetchImpl))
        .state,
      state,
    );
  }
  for (const version of [0, -1, 1.5, Number.NaN]) {
    const none = respond(body);
    assert.equal(
      (await exportApproval(BASE, "t", ...ids, version, none.fetchImpl)).state,
      "not_found",
    );
    assert.equal(none.seen.length, 0);
  }
});
