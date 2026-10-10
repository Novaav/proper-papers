import assert from "node:assert/strict";
import { test } from "node:test";

import {
  POLL,
  initialPollState,
  nextDelay,
  parseStatusAnswer,
  pollNotice,
  pollReducer,
  type StatusAnswer,
} from "./poll.ts";

const ok = (
  status: "queued" | "running" | "completed" | "needs_review" | "failed",
): StatusAnswer => ({
  state: "ok",
  status,
  attempts: 1,
  startedAt: null,
});

test("polls only while queued or running", () => {
  assert.equal(initialPollState("queued").stopped, null);
  assert.equal(initialPollState("running").stopped, null);
  for (const status of ["completed", "needs_review", "failed"] as const) {
    assert.equal(initialPollState(status).stopped, "final");
  }
});

test("a final status stops polling for good", () => {
  let state = initialPollState("queued");
  state = pollReducer(state, { answer: ok("running"), elapsedMs: 2_000 });
  assert.deepEqual(state, { status: "running", errors: 0, stopped: null });
  state = pollReducer(state, { answer: ok("needs_review"), elapsedMs: 4_000 });
  assert.equal(state.stopped, "final");
  // Nothing changes it afterwards.
  assert.deepEqual(
    pollReducer(state, { answer: ok("running"), elapsedMs: 6_000 }),
    state,
  );
});

test("errors back off and are noticed after three in a row; success resets", () => {
  let state = initialPollState("running");
  for (let i = 1; i <= 3; i++) {
    state = pollReducer(state, {
      answer: { state: "unavailable" },
      elapsedMs: i * 1000,
    });
  }
  assert.equal(state.errors, 3);
  assert.equal(pollNotice(state), "Couldn't check the status. Retrying…");
  state = pollReducer(state, { answer: ok("running"), elapsedMs: 5_000 });
  assert.equal(state.errors, 0);
  assert.equal(pollNotice(state), null);
});

test("delays: fast at first, then slow, backing off to a ceiling", () => {
  assert.equal(nextDelay(0, 0), POLL.fastMs);
  assert.equal(nextDelay(POLL.fastForMs, 0), POLL.slowMs);
  assert.equal(nextDelay(60_000, 1), POLL.slowMs * 2);
  assert.equal(nextDelay(60_000, 10), POLL.maxBackoffMs);
});

test("gives up after the limit, signed out and vanished runs stop", () => {
  const late = pollReducer(initialPollState("queued"), {
    answer: ok("queued"),
    elapsedMs: POLL.giveUpAfterMs,
  });
  assert.equal(late.stopped, "gave_up");
  assert.match(pollNotice(late) ?? "", /Still processing/);
  const out = pollReducer(initialPollState("queued"), {
    answer: { state: "signed_out" },
    elapsedMs: 1,
  });
  assert.equal(out.stopped, "signed_out");
  assert.match(pollNotice(out) ?? "", /Sign in again/);
  for (const state of ["none", "not_found", "not_clean"] as const) {
    assert.equal(
      pollReducer(initialPollState("running"), {
        answer: { state },
        elapsedMs: 1,
      }).stopped,
      "gone",
    );
  }
});

test("the status answer is parsed strictly", () => {
  assert.deepEqual(
    parseStatusAnswer({
      state: "ok",
      status: "running",
      attempts: 1,
      startedAt: "2026-09-30T10:00:00Z",
    }),
    {
      state: "ok",
      status: "running",
      attempts: 1,
      startedAt: "2026-09-30T10:00:00Z",
    },
  );
  for (const state of ["none", "signed_out", "not_found", "not_clean"]) {
    assert.deepEqual(parseStatusAnswer({ state }), { state });
  }
  for (const bad of [
    null,
    "ok",
    { state: "ok", status: "paused", attempts: 1, startedAt: null },
    { state: "ok", status: "running", attempts: "1", startedAt: null },
    { state: "ok", status: "running", attempts: 1, startedAt: "yesterday" },
    { state: "weird" },
  ]) {
    assert.deepEqual(parseStatusAnswer(bad), { state: "unavailable" });
  }
});
