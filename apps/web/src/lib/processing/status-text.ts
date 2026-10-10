// The processing sentences the browser shows while a run is still changing.
// Client-safe: it imports no API, authentication or document modules, so the
// live status component (processing-status.tsx) can use it; the server-rendered
// view (view.ts) uses the same sentences.

import { formatTime } from "../format.ts";

/** How the processing state is emphasised on the page. */
export type StatusTone = "neutral" | "working" | "attention" | "failed";

/** The sentence for a run that is still queued or running (server and client alike). */
export function liveStatusText(
  status: "queued" | "running",
  attempts: number,
  startedAt: string | null,
): string {
  if (status === "queued") {
    return attempts === 0
      ? "Queued — waiting to start."
      : `Waiting to retry (attempt ${attempts + 1} of 3).`;
  }
  return `Processing…${startedAt ? ` Started ${formatTime(startedAt)}.` : ""}${
    attempts > 1 ? ` Attempt ${attempts} of 3.` : ""
  }`;
}

/** Announced once when polling sees the run finish, before the page shows the result. */
export function finishedAnnouncement(
  status: "completed" | "needs_review" | "failed",
): string {
  switch (status) {
    case "completed":
      return "Processing finished. Showing the result.";
    case "needs_review":
      return "Processing finished: needs review. Showing the result.";
    case "failed":
      return "Processing failed. Showing the reason.";
  }
}
