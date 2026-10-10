import assert from "node:assert/strict";
import { test } from "node:test";

import type {
  ApprovalInput,
  ApprovalResult,
  DecisionInput,
  DecisionResult,
  ExportResult,
} from "../api/review.ts";
import type { Session } from "../auth/session.ts";
import {
  handleApproval,
  handleDecision,
  handleExport,
  type ApprovalDeps,
  type DecisionDeps,
  type ExportDeps,
} from "./handlers.ts";

const ORIGIN = "https://app.test";
const TENANT = "01a0e9bd-65a9-700c-a38b-c2210bcb6380";
const WORKSPACE = "01a0e9d1-6f65-7ac6-b4b3-84557cd2b17f";
const DOCUMENT = "01a0f000-0000-7000-8000-000000000001";
const RUN = "01a0f000-0000-7000-8000-00000000000a";
const PAGE = `/tenants/${TENANT}/workspaces/${WORKSPACE}/documents/${DOCUMENT}`;
const NOW = 1_800_000_000;
const OLD_SESSION: Session = {
  accessToken: "tok",
  expiresAt: NOW + 600,
  createdAt: NOW - 120,
};
const FRESH_SESSION: Session = { ...OLD_SESSION, createdAt: NOW - 1 };
const LOGIN = `${ORIGIN}/auth/login?returnTo=${encodeURIComponent(PAGE)}`;
const DECISION_FORM = `run_id=${RUN}&expected_seq=0&field_key=effective_date&resolution=candidate&candidate_ordinal=1`;
const APPROVAL_FORM = `run_id=${RUN}&expected_seq=2&expected_version=0`;

function post(
  body: string,
  origin: string | null = ORIGIN,
  type = "application/x-www-form-urlencoded",
) {
  const headers: Record<string, string> = { "Content-Type": type };
  if (origin !== null) headers.Origin = origin;
  return new Request(`${ORIGIN}${PAGE}/review/decisions`, {
    method: "POST",
    headers,
    body,
  });
}

function decisionDeps(
  result: DecisionResult,
  session: Session | null = OLD_SESSION,
) {
  const calls: DecisionInput[] = [];
  const logs: string[] = [];
  const deps: DecisionDeps = {
    appOrigin: ORIGIN,
    readSession: async () => session,
    decide: async (token, tenant, workspace, document, input) => {
      assert.deepEqual(
        [token, tenant, workspace, document],
        ["tok", TENANT, WORKSPACE, DOCUMENT],
      );
      calls.push(input);
      return result;
    },
    now: () => NOW,
    log: (message) => logs.push(message),
  };
  return { deps, calls, logs };
}

function approvalDeps(
  result: ApprovalResult,
  session: Session | null = OLD_SESSION,
) {
  const calls: ApprovalInput[] = [];
  const deps: ApprovalDeps = {
    appOrigin: ORIGIN,
    readSession: async () => session,
    approve: async (_token, _tenant, _workspace, _document, input) => {
      calls.push(input);
      return result;
    },
    now: () => NOW,
    log: () => {},
  };
  return { deps, calls };
}

const decide = (
  body: string,
  result: DecisionResult = { state: "decided" },
  request = post(body),
) => {
  const context = decisionDeps(result);
  return {
    context,
    response: handleDecision(
      request,
      TENANT,
      WORKSPACE,
      DOCUMENT,
      context.deps,
    ),
  };
};

test("a decision is sent with the validated values and returns to the field", async () => {
  const { context, response } = decide(DECISION_FORM);
  const answer = await response;
  assert.equal(answer.status, 303);
  assert.equal(
    answer.headers.get("location"),
    `${ORIGIN}${PAGE}?review=decided#field-effective-date`,
  );
  assert.equal(answer.headers.get("cache-control"), "no-store");
  assert.deepEqual(context.calls, [
    {
      runId: RUN,
      expectedSeq: 0,
      fieldKey: "effective_date",
      resolution: "candidate",
      candidateOrdinal: 1,
    },
  ]);
  const unknown = decide(
    `run_id=${RUN}&expected_seq=3&field_key=customer_name&resolution=unknown`,
  );
  assert.equal(
    (await unknown.response).headers.get("location"),
    `${ORIGIN}${PAGE}?review=decided#field-customer-name`,
  );
  assert.equal(unknown.context.calls[0]?.candidateOrdinal, null);
});

test("every API answer becomes a fixed code", async () => {
  const cases: [DecisionResult, string][] = [
    [{ state: "stale" }, "?review=stale#field-effective-date"],
    [{ state: "run_changed" }, "?review=run_changed#field-effective-date"],
    [{ state: "invalid" }, "?review=invalid#field-effective-date"],
    [{ state: "not_reviewer" }, "?review=not_reviewer#field-effective-date"],
    [{ state: "blocked" }, "?review=not_clean#field-effective-date"],
    [{ state: "not_scanned" }, "?review=not_clean#field-effective-date"],
    [{ state: "unavailable" }, "?review=unavailable#field-effective-date"],
    [{ state: "not_found" }, "#field-effective-date"],
  ];
  for (const [result, suffix] of cases) {
    const answer = await decide(DECISION_FORM, result).response;
    assert.equal(
      answer.headers.get("location"),
      `${ORIGIN}${PAGE}${suffix}`,
      result.state,
    );
  }
});

test("malformed or unexpected forms are invalid and never reach the API", async () => {
  const forms = [
    "",
    `run_id=${RUN}`,
    DECISION_FORM.replace(RUN, "not-a-uuid"),
    DECISION_FORM.replace("expected_seq=0", "expected_seq=-1"),
    DECISION_FORM.replace("expected_seq=0", "expected_seq=01"),
    DECISION_FORM.replace("expected_seq=0", "expected_seq=1e3"),
    DECISION_FORM.replace("effective_date", "notes"),
    DECISION_FORM.replace("resolution=candidate", "resolution=value"),
    DECISION_FORM.replace("&candidate_ordinal=1", ""), // candidate without ordinal
    `run_id=${RUN}&expected_seq=0&field_key=customer_name&resolution=unknown&candidate_ordinal=0`,
    `${DECISION_FORM}&candidate_ordinal=0`, // a field twice
    `${DECISION_FORM}&value=Acme`, // free text is never accepted
    `${DECISION_FORM}&x=${"a".repeat(3000)}`, // too large
  ];
  for (const form of forms) {
    const { context, response } = decide(form);
    const location = (await response).headers.get("location") ?? "";
    assert.match(location, /\?review=invalid/, form.slice(0, 60));
    assert.equal(context.calls.length, 0, form.slice(0, 60));
  }
  const json = decide(
    DECISION_FORM,
    { state: "decided" },
    post(DECISION_FORM, ORIGIN, "application/json"),
  );
  assert.match(
    (await json.response).headers.get("location") ?? "",
    /\?review=invalid/,
  );
  assert.equal(json.context.calls.length, 0);
});

test("a foreign or missing Origin is refused before anything else", async () => {
  for (const origin of [null, "https://evil.test", `${ORIGIN}.evil.test`]) {
    const { context, response } = decide(
      DECISION_FORM,
      { state: "decided" },
      post(DECISION_FORM, origin),
    );
    assert.equal((await response).status, 403);
    assert.equal(context.calls.length, 0);
    assert.deepEqual(context.logs, ["review write refused: origin_mismatch"]);
  }
});

test("a saved decision moves the review on to where the page said it goes next", async () => {
  const location = async (after: string) => {
    const { context, response } = decide(`${DECISION_FORM}&after=${after}`);
    const answer = await response;
    // `after` steers the redirect only: the API receives the decision alone.
    assert.deepEqual(context.calls, [
      {
        runId: RUN,
        expectedSeq: 0,
        fieldKey: "effective_date",
        resolution: "candidate",
        candidateOrdinal: 1,
      },
    ]);
    return answer.headers.get("location");
  };
  // The next field that still needs a decision.
  assert.equal(
    await location("customer_name"),
    `${ORIGIN}${PAGE}?review=decided#field-customer-name`,
  );
  // The last one was decided: on to the approval, which is never submitted for you.
  assert.equal(
    await location("approval"),
    `${ORIGIN}${PAGE}?review=decided#approval`,
  );
  // Nothing left to move to: stay on the decided field, as before.
  assert.equal(
    await location("stay"),
    `${ORIGIN}${PAGE}?review=decided#field-effective-date`,
  );
});

test("a decision that is refused or fails never advances", async () => {
  const refusals: [DecisionResult, string][] = [
    [{ state: "stale" }, "stale"],
    [{ state: "run_changed" }, "run_changed"],
    [{ state: "invalid" }, "invalid"],
    [{ state: "not_reviewer" }, "not_reviewer"],
    [{ state: "not_scanned" }, "not_clean"],
    [{ state: "unavailable" }, "unavailable"],
  ];
  for (const [result, code] of refusals) {
    for (const after of ["customer_name", "approval"]) {
      const { response } = decide(`${DECISION_FORM}&after=${after}`, result);
      assert.equal(
        (await response).headers.get("location"),
        `${ORIGIN}${PAGE}?review=${code}#field-effective-date`,
        `${result.state} with after=${after}`,
      );
    }
  }
  // A field that no longer exists answers with the uniform not-found page, still without moving.
  const gone = decide(`${DECISION_FORM}&after=customer_name`, {
    state: "not_found",
  });
  assert.equal(
    (await gone.response).headers.get("location"),
    `${ORIGIN}${PAGE}#field-effective-date`,
  );
});

test("an unknown 'after' value makes the form invalid and nothing is sent", async () => {
  for (const after of ["evil", "field-customer-name", "../x", "", "APPROVAL"]) {
    const { context, response } = decide(`${DECISION_FORM}&after=${after}`);
    assert.equal(
      (await response).headers.get("location"),
      `${ORIGIN}${PAGE}?review=invalid`,
      after,
    );
    assert.equal(context.calls.length, 0);
  }
  // Repeating the field is invalid too, like every other field.
  const twice = decide(`${DECISION_FORM}&after=approval&after=stay`);
  assert.equal(
    (await twice.response).headers.get("location"),
    `${ORIGIN}${PAGE}?review=invalid`,
  );
});

test("malformed IDs are 404; no session or no configuration never reaches the API", async () => {
  const bad = decisionDeps({ state: "decided" });
  const response = await handleDecision(
    post(DECISION_FORM),
    TENANT,
    WORKSPACE,
    "../x",
    bad.deps,
  );
  assert.equal(response.status, 404);
  const anonymous = decisionDeps({ state: "decided" }, null);
  const login = await handleDecision(
    post(DECISION_FORM),
    TENANT,
    WORKSPACE,
    DOCUMENT,
    anonymous.deps,
  );
  assert.equal(login.headers.get("location"), LOGIN);
  const unconfigured = decisionDeps({ state: "decided" });
  const off = await handleDecision(
    post(DECISION_FORM),
    TENANT,
    WORKSPACE,
    DOCUMENT,
    {
      ...unconfigured.deps,
      appOrigin: null,
    },
  );
  assert.equal(off.status, 503);
  for (const context of [bad, anonymous, unconfigured])
    assert.equal(context.calls.length, 0);
});

test("a rejected token: an older session signs in again, a fresh one sees rejected", async () => {
  const old = decisionDeps({ state: "unauthorized" });
  const answer = await handleDecision(
    post(DECISION_FORM),
    TENANT,
    WORKSPACE,
    DOCUMENT,
    old.deps,
  );
  assert.equal(answer.headers.get("location"), LOGIN);
  const fresh = decisionDeps({ state: "unauthorized" }, FRESH_SESSION);
  const rejected = await handleDecision(
    post(DECISION_FORM),
    TENANT,
    WORKSPACE,
    DOCUMENT,
    fresh.deps,
  );
  assert.equal(
    rejected.headers.get("location"),
    `${ORIGIN}${PAGE}?review=rejected#field-effective-date`,
  );
});

test("an approval is sent with the numbers the page saw and returns to the approval", async () => {
  const cases: [ApprovalResult, string][] = [
    [{ state: "approved", version: 1 }, "approved"],
    [{ state: "unresolved", fields: ["customer_name"] }, "unresolved"],
    [{ state: "stale" }, "stale"],
    [{ state: "run_changed" }, "run_changed"],
    [{ state: "not_reviewer" }, "not_reviewer"],
    [{ state: "blocked" }, "not_clean"],
    [{ state: "unavailable" }, "unavailable"],
  ];
  for (const [result, code] of cases) {
    const { deps, calls } = approvalDeps(result);
    const answer = await handleApproval(
      post(APPROVAL_FORM),
      TENANT,
      WORKSPACE,
      DOCUMENT,
      deps,
    );
    assert.equal(
      answer.headers.get("location"),
      `${ORIGIN}${PAGE}?review=${code}#approval`,
    );
    assert.deepEqual(calls, [
      { runId: RUN, expectedSeq: 2, expectedVersion: 0 },
    ]);
  }
  for (const form of [
    `run_id=${RUN}&expected_seq=2`,
    `${APPROVAL_FORM}&expected_seq=3`,
    APPROVAL_FORM.replace("expected_version=0", "expected_version=x"),
    `${APPROVAL_FORM}&field_key=provider_name`,
  ]) {
    const { deps, calls } = approvalDeps({ state: "approved", version: 1 });
    const answer = await handleApproval(
      post(form),
      TENANT,
      WORKSPACE,
      DOCUMENT,
      deps,
    );
    assert.equal(
      answer.headers.get("location"),
      `${ORIGIN}${PAGE}?review=invalid#approval`,
    );
    assert.equal(calls.length, 0);
  }
  const foreign = approvalDeps({ state: "approved", version: 1 });
  const refused = await handleApproval(
    post(APPROVAL_FORM, null),
    TENANT,
    WORKSPACE,
    DOCUMENT,
    foreign.deps,
  );
  assert.equal(refused.status, 403);
  assert.equal(foreign.calls.length, 0);
});

function exportDeps(
  result: ExportResult,
  session: Session | null = OLD_SESSION,
) {
  const calls: number[] = [];
  const logs: string[] = [];
  const deps: ExportDeps = {
    readSession: async () => session,
    export: async (_token, _tenant, _workspace, _document, version) => {
      calls.push(version);
      return result;
    },
    now: () => NOW,
    log: (message) => logs.push(message),
  };
  return { deps, calls, logs };
}

const upstream = (headers: Record<string, string>) =>
  new Response('{"schema":"approved_record.v1"}', { headers });

test("the export streams the record as a sandboxed attachment", async () => {
  const { deps, calls } = exportDeps({
    state: "ok",
    response: upstream({
      "Content-Disposition": 'attachment; filename="contract-approved-v2.json"',
      "X-Record-SHA256": "b".repeat(64),
      "Content-Length": "31",
      "Set-Cookie": "leak=1",
    }),
  });
  const answer = await handleExport(TENANT, WORKSPACE, DOCUMENT, "2", deps);
  assert.equal(answer.status, 200);
  assert.deepEqual(calls, [2]);
  assert.deepEqual(Object.fromEntries(answer.headers), {
    "cache-control": "no-store",
    "content-type": "application/json",
    "content-disposition": 'attachment; filename="contract-approved-v2.json"',
    "x-content-type-options": "nosniff",
    "content-security-policy": "sandbox; default-src 'none'",
    "x-record-sha256": "b".repeat(64),
    "content-length": "31",
  });
  assert.equal(await answer.text(), '{"schema":"approved_record.v1"}');
});

test("an export that is not an attachment, or a malformed digest, is not passed on", async () => {
  const inline = exportDeps({
    state: "ok",
    response: upstream({ "Content-Disposition": "inline" }),
  });
  assert.equal(
    (await handleExport(TENANT, WORKSPACE, DOCUMENT, "1", inline.deps)).status,
    502,
  );
  const odd = exportDeps({
    state: "ok",
    response: upstream({
      "Content-Disposition": "attachment",
      "X-Record-SHA256": "<script>",
    }),
  });
  const answer = await handleExport(TENANT, WORKSPACE, DOCUMENT, "1", odd.deps);
  assert.equal(answer.headers.get("x-record-sha256"), null);
});

test("export failures: fixed answers, malformed versions never reach the API", async () => {
  for (const version of ["0", "01", "-1", "1.0", "abc", "1000000000", ""]) {
    const { deps, calls } = exportDeps({ state: "unavailable" });
    assert.equal(
      (await handleExport(TENANT, WORKSPACE, DOCUMENT, version, deps)).status,
      404,
      version,
    );
    assert.equal(calls.length, 0);
  }
  const anonymous = exportDeps({ state: "unavailable" }, null);
  const login = await handleExport(
    TENANT,
    WORKSPACE,
    DOCUMENT,
    "1",
    anonymous.deps,
  );
  assert.equal(
    login.headers.get("location"),
    `/auth/login?returnTo=${encodeURIComponent(PAGE)}`,
  );
  assert.equal(anonymous.calls.length, 0);
  const cases: [ExportResult, number, string | null][] = [
    [{ state: "not_found" }, 404, null],
    [{ state: "blocked" }, 303, `${PAGE}?review=not_clean`],
    [{ state: "unavailable" }, 503, null],
  ];
  for (const [result, status, location] of cases) {
    const { deps } = exportDeps(result);
    const answer = await handleExport(TENANT, WORKSPACE, DOCUMENT, "1", deps);
    assert.equal(answer.status, status, result.state);
    assert.equal(answer.headers.get("location"), location);
  }
});

test("a form declared larger than a review form is refused without reading it", async () => {
  const body = new ReadableStream({
    pull() {
      throw new Error("the body must not be read");
    },
  });
  const request = new Request(`${ORIGIN}${PAGE}/review/decisions`, {
    method: "POST",
    headers: {
      Origin: ORIGIN,
      "Content-Type": "application/x-www-form-urlencoded",
      "Content-Length": "5000",
    },
    body,
    duplex: "half",
  } as RequestInit);
  const { deps, calls } = decisionDeps({ state: "decided" });
  const answer = await handleDecision(
    request,
    TENANT,
    WORKSPACE,
    DOCUMENT,
    deps,
  );
  assert.equal(
    answer.headers.get("location"),
    `${ORIGIN}${PAGE}?review=invalid`,
  );
  assert.equal(calls.length, 0);
});
