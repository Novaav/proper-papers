// When the document page checks the processing status again (pure, so the
// schedule is unit-tested; processing-status.tsx only runs it). The page polls
// only while a run is queued or running and stops for good at a final status.

import type { RunStatus } from "../api/processing.ts";

export const POLL = {
  fastMs: 2_000,
  fastForMs: 20_000,
  slowMs: 5_000,
  maxBackoffMs: 30_000,
  giveUpAfterMs: 10 * 60_000,
  errorsBeforeNotice: 3,
} as const;

/** What the status route answered, reduced to what polling needs. */
export type StatusAnswer =
  | {
      state: "ok";
      status: RunStatus;
      attempts: number;
      startedAt: string | null;
    }
  | { state: "none" }
  | { state: "signed_out" }
  | { state: "not_found" }
  | { state: "not_clean" }
  | { state: "unavailable" };

export interface PollState {
  status: RunStatus;
  /** Consecutive failed checks. */
  errors: number;
  /** Why polling ended; null while it continues. */
  stopped: "final" | "gave_up" | "signed_out" | "gone" | null;
}

export function initialPollState(status: RunStatus): PollState {
  return { status, errors: 0, stopped: isFinal(status) ? "final" : null };
}

export function isFinal(status: RunStatus): boolean {
  return (
    status === "completed" || status === "needs_review" || status === "failed"
  );
}

export function pollReducer(
  state: PollState,
  event: { answer: StatusAnswer; elapsedMs: number },
): PollState {
  if (state.stopped) return state;
  const { answer } = event;
  switch (answer.state) {
    case "ok":
      return {
        status: answer.status,
        errors: 0,
        stopped: isFinal(answer.status)
          ? "final"
          : event.elapsedMs >= POLL.giveUpAfterMs
            ? "gave_up"
            : null,
      };
    case "signed_out":
      return { ...state, stopped: "signed_out" };
    // The run or the document went away, or it is no longer clean: the page
    // must be loaded again to show what is true now.
    case "none":
    case "not_found":
    case "not_clean":
      return { ...state, stopped: "gone" };
    case "unavailable":
      return {
        ...state,
        errors: state.errors + 1,
        stopped: event.elapsedMs >= POLL.giveUpAfterMs ? "gave_up" : null,
      };
  }
}

/** Milliseconds until the next check. */
export function nextDelay(elapsedMs: number, errors: number): number {
  const base = elapsedMs < POLL.fastForMs ? POLL.fastMs : POLL.slowMs;
  return errors === 0 ? base : Math.min(POLL.maxBackoffMs, base * 2 ** errors);
}

/** A quiet note under the status while checks keep failing; null when all is well. */
export function pollNotice(state: PollState): string | null {
  if (state.stopped === "gave_up") {
    return "Still processing. Check the status again later.";
  }
  if (state.stopped === "signed_out") {
    return "Your session has ended. Sign in again to see the result.";
  }
  if (state.errors >= POLL.errorsBeforeNotice) {
    return "Couldn't check the status. Retrying…";
  }
  return null;
}

const STATUSES: readonly RunStatus[] = [
  "queued",
  "running",
  "completed",
  "needs_review",
  "failed",
];

/** The status route's JSON, checked; anything unexpected is "unavailable". */
export function parseStatusAnswer(value: unknown): StatusAnswer {
  if (typeof value !== "object" || value === null)
    return { state: "unavailable" };
  const answer = value as Record<string, unknown>;
  switch (answer.state) {
    case "ok":
      return STATUSES.includes(answer.status as RunStatus) &&
        typeof answer.attempts === "number" &&
        Number.isSafeInteger(answer.attempts) &&
        (answer.startedAt === null ||
          (typeof answer.startedAt === "string" &&
            !Number.isNaN(Date.parse(answer.startedAt))))
        ? {
            state: "ok",
            status: answer.status as RunStatus,
            attempts: answer.attempts,
            startedAt: answer.startedAt,
          }
        : { state: "unavailable" };
    case "none":
    case "signed_out":
    case "not_found":
    case "not_clean":
      return { state: answer.state };
    default:
      return { state: "unavailable" };
  }
}
