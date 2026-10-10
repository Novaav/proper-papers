import assert from "node:assert/strict";
import { test } from "node:test";

import type { ExtractionResult, Field } from "../api/processing.ts";
import type { Decision, ReviewState } from "../api/review.ts";
import { recordView } from "../processing/view.ts";
import { decisionTone, reviewView } from "./view.ts";

const RUN = "01a0f000-0000-7000-8000-00000000000a";
const OTHER_RUN = "01a0f000-0000-7000-8000-00000000000b";
const AT = "2026-10-01T10:00:00Z";
const quote = (text: string, page = 1) => ({ page, quote: text });
const missing: Field = {
  status: "unknown",
  reason: "not_found",
  candidates: [],
};

const RESULT: ExtractionResult = {
  outcome: "needs_review",
  extractor: "rules-1",
  fields: {
    provider_name: {
      status: "found",
      reason: null,
      candidates: [
        {
          value: "Northwind Analytics Ltd",
          raw: "Northwind Analytics Ltd",
          evidence: [quote("Provider: Northwind Analytics Ltd")],
        },
      ],
    },
    customer_name: missing, // required: needs review
    effective_date: {
      status: "conflict",
      reason: null,
      candidates: [
        {
          value: "2026-03-01",
          raw: "1 March 2026",
          evidence: [quote("Effective Date: 1 March 2026")],
        },
        {
          value: "2026-04-01",
          raw: "1 April 2026",
          evidence: [quote("as of 1 April 2026", 2)],
        },
        {
          value: null,
          raw: "03/04/2026",
          evidence: [quote("dated 03/04/2026", 3)],
        },
      ],
    },
    end_date: missing, // optional: approved as extracted, no decision offered
    governing_law: missing,
    contract_value: missing,
    notice_period_days: {
      status: "unknown",
      reason: "unparseable",
      candidates: [
        {
          value: null,
          raw: "three months",
          evidence: [quote("Notice: three months")],
        },
      ],
    },
  },
};
const RECORD = recordView(RESULT);

const decision = (
  seq: number,
  fieldKey: Decision["fieldKey"],
  ordinal: number | null,
): Decision => ({
  seq,
  fieldKey,
  resolution: ordinal === null ? "unknown" : "candidate",
  candidateOrdinal: ordinal,
  decidedAt: AT,
});
const state = (overrides: Partial<ReviewState> = {}): ReviewState => {
  const decisions = overrides.decisions ?? [];
  return {
    runId: RUN,
    canReview: true,
    decisionsThrough: decisions.length,
    decisions,
    approvals: [],
    ...overrides,
  };
};
const approval = (version: number, decisionsThrough: number, runId = RUN) => ({
  version,
  runId,
  decisionsThrough,
  approvedAt: AT,
  recordSha256: "a".repeat(64),
});

test("each field offers only the decisions it allows, named with the field", () => {
  const view = reviewView(RECORD, RUN, state());
  assert.ok(view);
  const actions = (key: keyof typeof view.fields) => {
    const field = view.fields[key];
    return {
      candidates: field.candidateActions.map((a) =>
        a ? `${a.label}${a.context}` : null,
      ),
      other: field.otherActions.map((a) => `${a.label}${a.context}`),
    };
  };
  assert.deepEqual(actions("provider_name"), {
    candidates: [null],
    other: [
      "Confirm Northwind Analytics Ltd for Provider",
      "Mark unknown: Provider",
    ],
  });
  assert.deepEqual(actions("effective_date"), {
    // The ambiguous mention has no value, so it cannot be chosen.
    candidates: [
      "Use this value: 1 March 2026 for Effective date",
      "Use this value: 1 April 2026 for Effective date",
      null,
    ],
    other: ["Neither — mark unknown: Effective date"],
  });
  assert.deepEqual(actions("customer_name"), {
    candidates: [],
    other: ["Confirm unknown: Customer"],
  });
  // Optional unknown fields are approved as extracted: nothing to decide.
  assert.deepEqual(actions("end_date"), { candidates: [], other: [] });
  assert.deepEqual(actions("notice_period_days"), {
    candidates: [null],
    other: [],
  });
  const ordinals = view.fields.effective_date.candidateActions.map(
    (a) => a?.candidateOrdinal ?? null,
  );
  assert.deepEqual(ordinals, [0, 1, null]);
});

test("the current decision is stated in words and not offered again", () => {
  const view = reviewView(
    RECORD,
    RUN,
    state({
      decisions: [
        decision(1, "effective_date", 0),
        decision(2, "effective_date", 1), // the latest decision counts
        decision(3, "customer_name", null),
        decision(4, "provider_name", null),
      ],
    }),
  );
  assert.ok(view);
  assert.equal(
    view.fields.effective_date.decisionText,
    "Decided: 1 April 2026",
  );
  assert.deepEqual(
    view.fields.effective_date.candidateActions.map(
      (a) => a?.candidateOrdinal ?? null,
    ),
    [0, null, null],
  );
  assert.equal(
    view.fields.customer_name.decisionText,
    "Decided: confirmed unknown",
  );
  assert.deepEqual(view.fields.customer_name.otherActions, []);
  assert.equal(
    view.fields.provider_name.decisionText,
    "Decided: marked unknown",
  );
  assert.deepEqual(
    view.fields.provider_name.otherActions.map((a) => a.label),
    ["Confirm"],
  );
  assert.equal(view.fields.end_date.decisionText, null);
  assert.deepEqual(
    [view.fields.effective_date.resolved, view.fields.provider_name.resolved],
    [true, false], // provider never needed review
  );
});

test("approval waits for every field that needs review", () => {
  const open = reviewView(RECORD, RUN, state());
  assert.ok(open);
  assert.deepEqual(
    open.unresolved.map((row) => row.key),
    ["customer_name", "effective_date"],
  );
  assert.equal(open.canApprove, false);
  const half = reviewView(
    RECORD,
    RUN,
    state({ decisions: [decision(1, "effective_date", 1)] }),
  );
  assert.deepEqual(
    half?.unresolved.map((row) => row.key),
    ["customer_name"],
  );
  const ready = reviewView(
    RECORD,
    RUN,
    state({
      decisions: [
        decision(1, "effective_date", 1),
        decision(2, "customer_name", null),
      ],
    }),
  );
  assert.ok(ready);
  assert.deepEqual(
    [
      ready.unresolved,
      ready.canApprove,
      ready.expectedSeq,
      ready.expectedVersion,
    ],
    [[], true, 2, 0],
  );
});

test("an approval of the current decisions is not offered again; a change since is", () => {
  const decisions = [
    decision(1, "effective_date", 1),
    decision(2, "customer_name", null),
  ];
  const current = reviewView(
    RECORD,
    RUN,
    state({ decisions, approvals: [approval(1, 2)] }),
  );
  assert.ok(current);
  assert.deepEqual(
    [current.canApprove, current.changedSinceApproval, current.versions],
    [false, false, [{ version: 1, approvedAt: AT, current: true }]],
  );
  const changed = reviewView(
    RECORD,
    RUN,
    state({
      decisions: [...decisions, decision(3, "effective_date", 0)],
      approvals: [approval(1, 2)],
    }),
  );
  assert.ok(changed);
  assert.deepEqual(
    [changed.canApprove, changed.changedSinceApproval, changed.expectedVersion],
    [true, true, 1],
  );
  const two = reviewView(
    RECORD,
    RUN,
    state({ decisions, approvals: [approval(1, 1), approval(2, 2)] }),
  );
  assert.deepEqual(
    two?.versions.map((v) => [v.version, v.current]),
    [
      [2, true],
      [1, false],
    ],
  );
  // A version of another run is never current.
  const otherRun = reviewView(
    RECORD,
    RUN,
    state({ decisions, approvals: [approval(1, 2, OTHER_RUN)] }),
  );
  assert.equal(otherRun?.changedSinceApproval, true);
});

test("members see decisions and versions but no actions", () => {
  const view = reviewView(
    RECORD,
    RUN,
    state({ canReview: false, decisions: [decision(1, "effective_date", 1)] }),
  );
  assert.ok(view);
  assert.equal(
    view.fields.effective_date.decisionText,
    "Decided: 1 April 2026",
  );
  for (const field of Object.values(view.fields)) {
    assert.deepEqual(field.otherActions, []);
    assert.ok(field.candidateActions.every((action) => action === null));
  }
  assert.equal(view.canApprove, false);
});

test("a review of another run offers nothing on this page", () => {
  assert.equal(reviewView(RECORD, RUN, state({ runId: OTHER_RUN })), null);
  assert.equal(reviewView(RECORD, RUN, state({ runId: null })), null);
});

test("a chosen value is verified, an acknowledged unknown is neutral, no decision has no tone", () => {
  const tones = (decisions: ReturnType<typeof decision>[]) => {
    const view = reviewView(RECORD, RUN, state({ decisions }));
    assert.ok(view);
    return {
      view,
      tone: (key: keyof typeof view.fields) => view.fields[key].decisionTone,
    };
  };
  const decided = tones([
    decision(1, "effective_date", 1), // a candidate value chosen
    decision(2, "customer_name", null), // a required field confirmed unknown
    decision(3, "provider_name", null), // a found value marked unknown
  ]);
  assert.equal(decided.tone("effective_date"), "value");
  assert.equal(decided.tone("customer_name"), "unknown");
  assert.equal(decided.tone("provider_name"), "unknown");
  // Untouched fields have no tone; a flagged one stays in the unresolved list.
  assert.equal(decided.tone("end_date"), null);
  const none = tones([]);
  assert.equal(none.tone("effective_date"), null);
  assert.equal(none.tone("customer_name"), null);
  assert.deepEqual(
    none.view.unresolved.map((row) => row.key),
    ["customer_name", "effective_date"],
  );
  assert.equal(none.view.fields.customer_name.resolved, false);
});

test("the latest decision decides the tone, so changing your mind restores the right appearance", () => {
  const latest = (decisions: ReturnType<typeof decision>[]) => {
    const view = reviewView(RECORD, RUN, state({ decisions }));
    assert.ok(view);
    return view.fields.effective_date;
  };
  assert.equal(
    latest([decision(1, "effective_date", 0)]).decisionTone,
    "value",
  );
  // Value, then "Neither": no longer a verified value.
  const toUnknown = latest([
    decision(1, "effective_date", 0),
    decision(2, "effective_date", null),
  ]);
  assert.equal(toUnknown.decisionTone, "unknown");
  assert.equal(toUnknown.decisionText, "Decided: marked unknown");
  // Unknown, then a value again: verified.
  const back = latest([
    decision(1, "effective_date", null),
    decision(2, "effective_date", 1),
  ]);
  assert.equal(back.decisionTone, "value");
  assert.equal(back.decisionText, "Decided: 1 April 2026");
});

test("the tone follows the decision only: the resolution alone maps it", () => {
  assert.equal(decisionTone(null), null);
  assert.equal(decisionTone(decision(1, "end_date", 0)), "value");
  assert.equal(decisionTone(decision(1, "end_date", null)), "unknown");
});
