import assert from "node:assert/strict";
import { test } from "node:test";

import { afterDecision } from "./advance.ts";

const rows = [
  { id: "field-provider-name", key: "provider_name" },
  { id: "field-customer-name", key: "customer_name" },
  { id: "field-effective-date", key: "effective_date" },
  { id: "field-end-date", key: "end_date" },
];
const [provider, customer, effective, end] = rows as [
  (typeof rows)[number],
  (typeof rows)[number],
  (typeof rows)[number],
  (typeof rows)[number],
];

test("a decision moves on to the next pending field in record order", () => {
  assert.equal(afterDecision(customer, rows, [customer, end]), "end_date");
  assert.equal(
    afterDecision(customer, rows, [customer, effective, end]),
    "effective_date",
  );
});

test("it wraps round to an earlier pending field", () => {
  assert.equal(afterDecision(end, rows, [customer, end]), "customer_name");
});

test("the last pending field leads to the approval, never to an approval itself", () => {
  assert.equal(afterDecision(effective, rows, [effective]), "approval");
});

test("with nothing pending, even a changed mind about a decided field goes to the approval", () => {
  // The case found in the real app: a record already "Ready for approval",
  // one field's decision changed again: the review used to stay on the field.
  assert.equal(afterDecision(provider, rows, []), "approval");
  assert.equal(afterDecision(effective, rows, []), "approval");
});

test("changing a decided field while other fields are still pending goes to the next pending one", () => {
  // `provider` is already decided (not in `needs`); the pending ones are the next stop.
  assert.equal(afterDecision(provider, rows, [customer, end]), "customer_name");
  assert.equal(afterDecision(end, rows, [customer, end]), "customer_name");
});
