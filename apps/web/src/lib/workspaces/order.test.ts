import assert from "node:assert/strict";
import { test } from "node:test";

import { newestFirst } from "./order.ts";

const ws = (id: string, createdAt: string) => ({ id, createdAt });

test("workspaces are shown newest first without changing the input", () => {
  const input = [
    ws("a", "2026-09-28T21:00:00Z"),
    ws("b", "2026-09-30T08:00:00Z"),
    ws("c", "2026-09-29T08:00:00Z"),
  ];
  assert.deepEqual(
    newestFirst(input).map((w) => w.id),
    ["b", "c", "a"],
  );
  assert.deepEqual(
    input.map((w) => w.id),
    ["a", "b", "c"],
  );
});

test("equal or unreadable times keep the API's order", () => {
  const same = [
    ws("a", "2026-09-28T21:00:00Z"),
    ws("b", "2026-09-28T21:00:00Z"),
  ];
  assert.deepEqual(
    newestFirst(same).map((w) => w.id),
    ["a", "b"],
  );
  const odd = [ws("a", "not a date"), ws("b", "2026-09-28T21:00:00Z")];
  assert.deepEqual(
    newestFirst(odd).map((w) => w.id),
    ["a", "b"],
  );
  assert.deepEqual(newestFirst([]), []);
});
