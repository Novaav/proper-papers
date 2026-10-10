// What the document page shows for review (R1): per field, the current
// decision in words and the decisions a reviewer can make; for the record,
// whether it can be approved and which approved versions exist. Pure functions
// of the API's answers; the API re-checks every write.
//
// Decisions only ever point at an extracted candidate with a value, or set the
// field to UNKNOWN: there is no free text. Fields that need review are the
// rows the processing view marks (every conflict, every required field
// without a value); the others are approved as extracted, so an unknown
// optional field offers no decision.

import type { Approval, Decision, ReviewState } from "../api/review.ts";
import type { FieldKey } from "../api/processing.ts";
import type { FieldRowView, RecordView } from "../processing/view.ts";

export interface ReviewAction {
  /** The visible text of the button. */
  label: string;
  /** Visually hidden text completing the accessible name ("… for Provider"). */
  context: string;
  resolution: "candidate" | "unknown";
  candidateOrdinal: number | null;
}

/**
 * How a decision is shown: a chosen value is verified (green); an unknown the
 * reviewer acknowledged is not a verified value (neutral). No decision at all
 * is not a tone: a field that needs one keeps its amber "Needs review".
 */
export type DecisionTone = "value" | "unknown";

export interface FieldReview {
  key: FieldKey;
  /** The current decision in words, or null. */
  decisionText: string | null;
  /** Colour semantics of that decision, or null while there is none. */
  decisionTone: DecisionTone | null;
  /** A field that needed review and now has a decision. */
  resolved: boolean;
  /** Decisions offered next to the field (one per candidate for a conflict). */
  candidateActions: (ReviewAction | null)[];
  /** "Mark unknown" style actions, shown after the candidates. */
  otherActions: ReviewAction[];
}

export interface ApprovalVersion {
  version: number;
  approvedAt: string;
  /** Approved from the decisions now shown (same run, nothing decided since). */
  current: boolean;
}

export interface ReviewView {
  canReview: boolean;
  runId: string;
  /** The numbers the page saw, sent with every write (stale writes are refused). */
  expectedSeq: number;
  expectedVersion: number;
  fields: Record<FieldKey, FieldReview>;
  /** Fields that need review and have no decision yet, in record order. */
  unresolved: FieldRowView[];
  /** Approved versions, newest first. */
  versions: ApprovalVersion[];
  /** The newest version no longer matches the decisions (decided since). */
  changedSinceApproval: boolean;
  /** Approve is offered: a reviewer, nothing unresolved, no current version. */
  canApprove: boolean;
}

/**
 * The review of `record`, or null when the API's reviewable run is not the run
 * the page shows (then the page offers no review actions).
 */
export function reviewView(
  record: RecordView,
  runId: string,
  review: ReviewState,
): ReviewView | null {
  if (review.runId !== runId) return null;
  const latest = latestDecisions(review.decisions);
  const fields = {} as Record<FieldKey, FieldReview>;
  for (const row of record.rows) {
    fields[row.key] = fieldReview(
      row,
      latest.get(row.key) ?? null,
      review.canReview,
    );
  }
  const unresolved = record.rows.filter(
    (row) => row.attention && !latest.has(row.key),
  );
  const versions = [...review.approvals]
    .reverse()
    .map((approval) => version(approval, runId, review.decisionsThrough));
  const newest = versions[0];
  const changedSinceApproval = newest !== undefined && !newest.current;
  return {
    canReview: review.canReview,
    runId,
    expectedSeq: review.decisionsThrough,
    expectedVersion: review.approvals.length,
    fields,
    unresolved,
    versions,
    changedSinceApproval,
    canApprove:
      review.canReview &&
      unresolved.length === 0 &&
      (newest === undefined || changedSinceApproval),
  };
}

function latestDecisions(decisions: Decision[]): Map<FieldKey, Decision> {
  const latest = new Map<FieldKey, Decision>();
  for (const decision of decisions) latest.set(decision.fieldKey, decision);
  return latest;
}

function version(
  approval: Approval,
  runId: string,
  decisionsThrough: number,
): ApprovalVersion {
  return {
    version: approval.version,
    approvedAt: approval.approvedAt,
    current:
      approval.runId === runId &&
      approval.decisionsThrough === decisionsThrough,
  };
}

export function fieldReview(
  row: FieldRowView,
  decision: Decision | null,
  canReview: boolean,
): FieldReview {
  const decisionText = decision === null ? null : describe(row, decision);
  const review: FieldReview = {
    key: row.key,
    decisionText,
    decisionTone: decisionTone(decision),
    resolved: row.attention && decision !== null,
    candidateActions: row.candidates.map(() => null),
    otherActions: [],
  };
  if (!canReview) return review;
  const chosen = (ordinal: number) =>
    decision?.resolution === "candidate" &&
    decision.candidateOrdinal === ordinal;
  const unknownChosen = decision?.resolution === "unknown";
  const label = row.label;

  if (row.status === "found") {
    if (!chosen(0)) {
      review.otherActions.push({
        label: "Confirm",
        context: ` ${row.candidates[0]?.valueText ?? ""} for ${label}`,
        resolution: "candidate",
        candidateOrdinal: 0,
      });
    }
    if (!unknownChosen) {
      review.otherActions.push({
        label: "Mark unknown",
        context: `: ${label}`,
        resolution: "unknown",
        candidateOrdinal: null,
      });
    }
    return review;
  }
  if (row.status === "conflict") {
    review.candidateActions = row.candidates.map((candidate, ordinal) =>
      candidate.resolved && !chosen(ordinal)
        ? {
            label: "Use this value",
            context: `: ${candidate.valueText} for ${label}`,
            resolution: "candidate",
            candidateOrdinal: ordinal,
          }
        : null,
    );
    if (!unknownChosen) {
      review.otherActions.push({
        label: "Neither — mark unknown",
        context: `: ${label}`,
        resolution: "unknown",
        candidateOrdinal: null,
      });
    }
    return review;
  }
  // Unknown: only a field that needs review (required) asks for a decision.
  if (row.attention && !unknownChosen) {
    review.otherActions.push({
      label: "Confirm unknown",
      context: `: ${label}`,
      resolution: "unknown",
      candidateOrdinal: null,
    });
  }
  return review;
}

export function decisionTone(decision: Decision | null): DecisionTone | null {
  if (decision === null) return null;
  return decision.resolution === "candidate" ? "value" : "unknown";
}

function describe(row: FieldRowView, decision: Decision): string {
  if (decision.resolution === "candidate") {
    const candidate = row.candidates[decision.candidateOrdinal ?? -1];
    return candidate
      ? `Decided: ${candidate.valueText}`
      : "Decided: a value that is no longer shown";
  }
  return row.status === "unknown"
    ? "Decided: confirmed unknown"
    : "Decided: marked unknown";
}
