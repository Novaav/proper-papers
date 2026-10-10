import assert from "node:assert/strict";
import { test } from "node:test";

import type {
  ExtractionResult,
  Field,
  ProcessingRun,
} from "../api/processing.ts";
import {
  failureMessage,
  finishedAnnouncement,
  liveStatusText,
  fieldRow,
  formatValue,
  recordView,
  statusView,
} from "./view.ts";

const quote = (text: string, page = 1) => ({ page, quote: text });
const found = (
  value: Field["candidates"][number]["value"],
  raw: string,
  ...quotes: string[]
): Field => ({
  status: "found",
  reason: null,
  candidates: [{ value, raw, evidence: quotes.map((q) => quote(q)) }],
});
const missing: Field = {
  status: "unknown",
  reason: "not_found",
  candidates: [],
};

const RESULT: ExtractionResult = {
  outcome: "needs_review",
  extractor: "rules-1",
  fields: {
    provider_name: found(
      "Northwind Analytics Ltd",
      "Northwind Analytics Ltd",
      "Provider: Northwind Analytics Ltd",
    ),
    customer_name: missing,
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
          evidence: [quote("effective as of 1 April 2026", 2)],
        },
      ],
    },
    end_date: missing,
    governing_law: missing,
    contract_value: found(
      { amount: "120000.00", currency: "EUR" },
      "EUR 120,000.00",
      "Contract Value: EUR 120,000.00",
    ),
    notice_period_days: {
      status: "unknown",
      reason: "unparseable",
      candidates: [
        {
          value: null,
          raw: "three months",
          evidence: [quote("Notice Period: three months")],
        },
      ],
    },
  },
};

test("found fields are quiet: value, pages, closed", () => {
  const row = fieldRow("provider_name", RESULT.fields.provider_name);
  assert.deepEqual(
    [row.valueText, row.sourceText, row.attention, row.open, row.id],
    ["Northwind Analytics Ltd", "Page 1", false, false, "field-provider-name"],
  );
  assert.deepEqual(row.candidates[0]?.quotes, [
    {
      page: 1,
      before: "Provider: ",
      marked: "Northwind Analytics Ltd",
      after: "",
    },
  ]);
});

test("the same value quoted twice, or on several pages", () => {
  const twice = fieldRow("effective_date", {
    status: "found",
    reason: null,
    candidates: [
      {
        value: "2026-03-01",
        raw: "1 March 2026",
        evidence: [
          quote("Effective Date: 1 March 2026"),
          quote("effective as of 1 March 2026"),
        ],
      },
    ],
  });
  assert.equal(twice.sourceText, "Page 1 · 2 quotes");
  assert.equal(twice.candidates[0]?.quotes.length, 2);
  const pages = fieldRow("effective_date", {
    status: "found",
    reason: null,
    candidates: [
      {
        value: "2026-03-01",
        raw: "1 March 2026",
        evidence: [quote("1 March 2026", 3), quote("1 March 2026", 1)],
      },
    ],
  });
  assert.equal(pages.sourceText, "Pages 1, 3");
});

test("unknown: not found, required or optional", () => {
  const customer = fieldRow("customer_name", missing);
  assert.deepEqual(
    [
      customer.valueText,
      customer.sourceText,
      customer.attention,
      customer.attentionText,
    ],
    ["Not found in the document", null, true, "not found (required)"],
  );
  const law = fieldRow("governing_law", missing);
  assert.deepEqual(
    [law.attention, law.attentionText, law.open],
    [false, null, false],
  );
});

test("unknown: unparseable and ambiguous show their evidence, never a value", () => {
  const notice = fieldRow(
    "notice_period_days",
    RESULT.fields.notice_period_days,
  );
  assert.deepEqual(
    [notice.valueText, notice.open, notice.attention, notice.sourceText],
    ["Found, but not a valid number of days", true, false, "Page 1"],
  );
  assert.equal(
    notice.candidates[0]?.valueText,
    "“three months” (not a valid number of days)",
  );
  assert.equal(notice.candidates[0]?.resolved, false);
  const date = fieldRow("effective_date", {
    status: "unknown",
    reason: "ambiguous",
    candidates: [
      {
        value: null,
        raw: "03/04/2026",
        evidence: [quote("Effective Date: 03/04/2026")],
      },
    ],
  });
  assert.deepEqual(
    [date.valueText, date.attention, date.attentionText, date.open],
    [
      "Ambiguous: it can be read in more than one way",
      true,
      "ambiguous (required)",
      true,
    ],
  );
});

test("conflict: every candidate with its own evidence, none preferred", () => {
  const row = fieldRow("effective_date", RESULT.fields.effective_date);
  assert.deepEqual(
    [row.valueText, row.sourceText, row.attention, row.open, row.attentionText],
    ["2 different values", "Compare 2", true, true, "2 different values found"],
  );
  assert.deepEqual(
    row.candidates.map((c) => [c.valueText, c.quotes.map((q) => q.page)]),
    [
      ["1 March 2026", [1]],
      ["1 April 2026", [2]],
    ],
  );
});

test("a conflict with an unreadable mention keeps it as a candidate", () => {
  const row = fieldRow("effective_date", {
    status: "conflict",
    reason: null,
    candidates: [
      {
        value: "2026-03-01",
        raw: "1 March 2026",
        evidence: [quote("Effective Date: 1 March 2026")],
      },
      {
        value: null,
        raw: "03/04/2026",
        evidence: [quote("Effective Date: 03/04/2026")],
      },
    ],
  });
  assert.deepEqual(
    row.candidates.map((c) => [c.valueText, c.resolved]),
    [
      ["1 March 2026", true],
      ["“03/04/2026” (not a valid date)", false],
    ],
  );
});

test("record: attention rows in field order, found count", () => {
  const record = recordView(RESULT);
  assert.deepEqual(
    record.attention.map((row) => [row.label, row.attentionText]),
    [
      ["Customer", "not found (required)"],
      ["Effective date", "2 different values found"],
    ],
  );
  assert.equal(record.found, 2);
  assert.equal(record.rows.length, 7);
});

test("values are formatted without guessing", () => {
  assert.equal(formatValue("effective_date", "2026-03-01"), "1 March 2026");
  assert.equal(formatValue("end_date", "2029-12-31"), "31 December 2029");
  assert.equal(
    formatValue("contract_value", { amount: "1250000.00", currency: "USD" }),
    "USD 1,250,000.00",
  );
  assert.equal(
    formatValue("contract_value", {
      amount: "999999999999999.99",
      currency: "EUR",
    }),
    "EUR 999,999,999,999,999.99",
  );
  assert.equal(
    formatValue("contract_value", { amount: "0.50", currency: "GBP" }),
    "GBP 0.50",
  );
  assert.equal(formatValue("notice_period_days", 90), "90 days");
  assert.equal(formatValue("notice_period_days", 1), "1 day");
  assert.equal(
    formatValue("governing_law", "England and Wales"),
    "England and Wales",
  );
});

const RUN: ProcessingRun = {
  id: "01a0f000-0000-7000-8000-00000000000a",
  status: "queued",
  failureCode: null,
  attempts: 0,
  pipelineVersion: "p",
  createdAt: "2026-09-30T10:00:00Z",
  startedAt: null,
  finishedAt: null,
};

test("status line for every state, in words", () => {
  const record = recordView(RESULT);
  const cases: [ProcessingRun | null, string, string, boolean][] = [
    [null, "neutral", "Not processed yet.", false],
    [RUN, "working", "Queued — waiting to start.", true],
    [
      { ...RUN, attempts: 1 },
      "working",
      "Waiting to retry (attempt 2 of 3).",
      true,
    ],
    [
      {
        ...RUN,
        status: "running",
        attempts: 1,
        startedAt: "2026-09-30T10:00:01Z",
      },
      "working",
      "Processing… Started 30 Sept 2026, 10:00 UTC.",
      true,
    ],
    [
      { ...RUN, status: "running", attempts: 2, startedAt: null },
      "working",
      "Processing… Attempt 2 of 3.",
      true,
    ],
    [
      {
        ...RUN,
        status: "completed",
        attempts: 1,
        finishedAt: "2026-09-30T10:00:03Z",
      },
      "neutral",
      "Processed 30 Sept 2026, 10:00 UTC.",
      false,
    ],
    [
      { ...RUN, status: "needs_review", attempts: 1 },
      "attention",
      "Needs review — 2 fields.",
      false,
    ],
    [
      { ...RUN, status: "failed", failureCode: "embedded_content" },
      "failed",
      failureMessage("embedded_content"),
      false,
    ],
  ];
  for (const [run, tone, text, live] of cases) {
    assert.deepEqual(
      statusView(run, record),
      { tone, text, live },
      run?.status ?? "none",
    );
  }
});

test("every failure code has a fixed message; unknown codes get the generic one", () => {
  for (const code of [
    "no_text_layer",
    "embedded_content",
    "encrypted_pdf",
    "parse_failed",
    "too_many_pages",
    "text_too_large",
    "input_too_large",
    "timeout",
    "memory_exceeded",
    "not_clean",
    "integrity_mismatch",
  ]) {
    assert.match(failureMessage(code), /^Processing failed: /);
    assert.doesNotMatch(failureMessage(code), /processing service/);
  }
  for (const code of [
    "sandbox_unavailable",
    "worker_lost",
    "output_invalid",
    "extraction_failed",
    "made_up",
    null,
  ]) {
    assert.equal(
      failureMessage(code),
      "Processing failed: the processing service could not complete this document.",
    );
  }
});

test("live and finished sentences are the same on server and client", () => {
  assert.equal(liveStatusText("queued", 0, null), "Queued — waiting to start.");
  assert.equal(
    liveStatusText("queued", 2, null),
    "Waiting to retry (attempt 3 of 3).",
  );
  assert.equal(liveStatusText("running", 1, null), "Processing…");
  assert.equal(
    finishedAnnouncement("needs_review"),
    "Processing finished: needs review. Showing the result.",
  );
  assert.equal(
    finishedAnnouncement("failed"),
    "Processing failed. Showing the reason.",
  );
});
