"use client";

import { useRouter } from "next/navigation.js";
import { useEffect, useState } from "react";

import {
  DocumentScan,
  LoadingTrack,
} from "../../../../../../loading-states.tsx";
import {
  initialPollState,
  isFinal,
  nextDelay,
  parseStatusAnswer,
  pollNotice,
  pollReducer,
  type PollState,
} from "@/lib/processing/poll.ts";
import {
  finishedAnnouncement,
  liveStatusText,
  type StatusTone,
} from "@/lib/processing/status-text.ts";

// The document's processing state. It is rendered in every state so that the
// live region (role="status") stays the same DOM node when the page is
// rendered again: changes are announced politely, without moving focus. While
// a run is queued or running (`live`), the state is checked again on the
// schedule in poll.ts (the server renders the same sentence first, with a
// "Check status" link that works without JavaScript). When the run finishes,
// this says so and the page is rendered again on the server, which shows the
// result and replaces the text: the browser never receives results through
// this check.
export function ProcessingStatus({
  statusUrl,
  live,
  tone,
  text,
}: {
  statusUrl: string;
  live: "queued" | "running" | null;
  tone: StatusTone;
  text: string;
}) {
  const router = useRouter();
  // What the checks found, for the server text they replace; a new server text
  // (after a refresh) takes over again.
  const [polled, setPolled] = useState<{
    over: string;
    message: string;
    notice: string | null;
    /** The run's status as the last check reported it. */
    status: "queued" | "running" | null;
  } | null>(null);
  const shown = polled?.over === text ? polled : null;

  useEffect(() => {
    if (!live) return;
    let state: PollState = initialPollState(live);
    let message = text;
    const started = Date.now();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let cancelled = false;

    const check = async () => {
      if (document.hidden) {
        // Paused while the tab is hidden; checked again once it is visible.
        timer = setTimeout(check, 1000);
        return;
      }
      controller = new AbortController();
      let answer;
      try {
        const response = await fetch(statusUrl, {
          cache: "no-store",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        answer = parseStatusAnswer(await response.json());
      } catch {
        if (cancelled) return;
        answer = parseStatusAnswer(null);
      }
      if (cancelled) return;
      const elapsedMs = Date.now() - started;
      state = pollReducer(state, { answer, elapsedMs });
      if (answer.state === "ok" && !isFinal(answer.status)) {
        message = liveStatusText(
          answer.status as "queued" | "running",
          answer.attempts,
          answer.startedAt,
        );
      }
      if (state.stopped === "final" && answer.state === "ok") {
        message = finishedAnnouncement(
          answer.status as "completed" | "needs_review" | "failed",
        );
      }
      setPolled({
        over: text,
        message,
        notice: pollNotice(state),
        status:
          answer.state === "ok" &&
          (answer.status === "queued" || answer.status === "running")
            ? answer.status
            : null,
      });
      if (state.stopped === "final" || state.stopped === "gone") {
        router.refresh();
        return;
      }
      if (state.stopped) return;
      timer = setTimeout(check, nextDelay(elapsedMs, state.errors));
    };

    timer = setTimeout(check, nextDelay(0, 0));
    return () => {
      cancelled = true;
      clearTimeout(timer);
      controller?.abort();
    };
  }, [statusUrl, live, text, router]);

  // The miniature page follows the status the last check reported (still waiting
  // or being read); it is gone once the run has finished.
  const glyph = live ? (shown ? shown.status : live) : null;

  return (
    <dd className={`tone-${tone}`}>
      <span role="status">{shown?.message ?? text}</span>
      {glyph && <DocumentScan state={glyph} />}
      {glyph && <LoadingTrack state={glyph} />}
      {shown?.notice && (
        <span className="pipeline-notice"> {shown.notice}</span>
      )}
    </dd>
  );
}
