import assert from "node:assert/strict";
import { test } from "node:test";

import { isCanonicalUuid } from "./ids.ts";

test("canonical lowercase UUIDs are accepted", () => {
  assert.equal(isCanonicalUuid("01a0e9bd-65a9-700c-a38b-c2210bcb6380"), true);
});

test("everything else is rejected", () => {
  for (const value of [
    undefined,
    null,
    42,
    "",
    "not-a-uuid",
    "00000000-0000-0000-0000-000000000000",
    "01A0E9BD-65A9-700C-A38B-C2210BCB6380",
    "{01a0e9bd-65a9-700c-a38b-c2210bcb6380}",
    "01a0e9bd65a9700ca38bc2210bcb6380",
    "01a0e9bd-65a9-700c-a38b-c2210bcb6380/..",
    "../01a0e9bd-65a9-700c-a38b-c2210bcb6380",
    "01a0e9bd-65a9-700c-a38b-c2210bcb638",
    " 01a0e9bd-65a9-700c-a38b-c2210bcb6380",
    "01a0e9bd-65a9-700c-a38b-c2210bcb6380\n",
    "01a0e9bd-65a9-700c-a38b-c2210bcb638١",
  ]) {
    assert.equal(isCanonicalUuid(value), false, JSON.stringify(value));
  }
});
