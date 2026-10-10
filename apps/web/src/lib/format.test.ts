import assert from "node:assert/strict";
import { test } from "node:test";

import { formatDate, formatTime } from "./format.ts";

test("a date alone, in UTC, for dense lists", () => {
  assert.equal(formatDate("2026-10-09T23:30:00+00:00"), "9 Oct 2026");
  // 01:30 at UTC+2 is still the 9th in UTC: the list shows UTC like the rest.
  assert.equal(formatDate("2026-10-10T01:30:00+02:00"), "9 Oct 2026");
});

test("the full time stays available and says UTC", () => {
  assert.equal(
    formatTime("2026-10-09T08:05:00+00:00"),
    "9 Oct 2026, 08:05 UTC",
  );
});
