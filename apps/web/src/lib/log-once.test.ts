import assert from "node:assert/strict";
import { test } from "node:test";

import { createOnceLogger } from "./log-once.ts";

test("each distinct message is logged once", () => {
  const lines: string[] = [];
  const log = createOnceLogger((message) => lines.push(message));

  for (let i = 0; i < 5; i += 1) log("authentication is not configured: A");
  log("authentication is not configured: B");
  log("authentication is not configured: A");

  assert.deepEqual(lines, [
    "authentication is not configured: A",
    "authentication is not configured: B",
  ]);
});
