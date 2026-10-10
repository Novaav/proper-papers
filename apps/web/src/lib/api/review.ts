// R1 review endpoints of the API, called server-side with the session's access
// token. The API (and its database) decides who may review, numbers decisions,
// checks staleness and builds approved records from stored rows only; this
// module relays requests built from validated values and parses responses
// strictly. Anything unexpected is "unavailable", never guessed.

import { isCanonicalUuid } from "../ids.ts";
import {
  call,
  detail,
  documentsPath,
  failed,
  json,
  type Failure,
} from "./documents.ts";
import { FIELD_KEYS, type FieldKey } from "./processing.ts";

export interface Decision {
  seq: number;
  fieldKey: FieldKey;
  resolution: "candidate" | "unknown";
  candidateOrdinal: number | null;
  decidedAt: string;
}

export interface Approval {
  version: number;
  runId: string;
  decisionsThrough: number;
  approvedAt: string;
  recordSha256: string;
}

export interface ReviewState {
  /** The reviewable run (newest, completed or needs review), or null. */
  runId: string | null;
  canReview: boolean;
  decisionsThrough: number;
  decisions: Decision[];
  approvals: Approval[];
}

type NotClean = { state: "not_scanned" } | { state: "blocked" };

export type ReviewResult =
  { state: "ok"; review: ReviewState } | NotClean | Failure;

/** Refusals of a review write, each a fixed state (the API's detail). */
type WriteRefusal =
  | { state: "not_reviewer" }
  | { state: "stale" }
  | { state: "run_changed" }
  | { state: "invalid" };

export type DecisionResult =
  { state: "decided" } | WriteRefusal | NotClean | Failure;

export type ApprovalResult =
  | { state: "approved"; version: number }
  | { state: "unresolved"; fields: FieldKey[] }
  | WriteRefusal
  | NotClean
  | Failure;

/** On success the API's response is passed through unread (streamed). */
export type ExportResult =
  { state: "ok"; response: Response } | NotClean | Failure;

export interface DecisionInput {
  runId: string;
  expectedSeq: number;
  fieldKey: FieldKey;
  resolution: "candidate" | "unknown";
  candidateOrdinal: number | null;
}

export interface ApprovalInput {
  runId: string;
  expectedSeq: number;
  expectedVersion: number;
}

export async function getReview(
  baseUrl: string | undefined,
  accessToken: string,
  tenantId: string,
  workspaceId: string,
  documentId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<ReviewResult> {
  if (![tenantId, workspaceId, documentId].every(isCanonicalUuid)) {
    return { state: "not_found" };
  }
  const response = await call(
    baseUrl,
    reviewPath(tenantId, workspaceId, documentId),
    accessToken,
    fetchImpl,
  );
  const notClean = await notCleanState(response);
  if (notClean) return notClean;
  const failure = failed(response, 200);
  if (failure) return failure;
  const review = parseReview(await json(response as Response));
  return review ? { state: "ok", review } : { state: "unavailable" };
}

export async function postDecision(
  baseUrl: string | undefined,
  accessToken: string,
  tenantId: string,
  workspaceId: string,
  documentId: string,
  input: DecisionInput,
  fetchImpl: typeof fetch = fetch,
): Promise<DecisionResult> {
  if (![tenantId, workspaceId, documentId].every(isCanonicalUuid)) {
    return { state: "not_found" };
  }
  const body: Record<string, unknown> = {
    run_id: input.runId,
    expected_seq: input.expectedSeq,
    field_key: input.fieldKey,
    resolution: input.resolution,
  };
  if (input.candidateOrdinal !== null) {
    body.candidate_ordinal = input.candidateOrdinal;
  }
  const response = await post(
    baseUrl,
    `${reviewPath(tenantId, workspaceId, documentId)}/decisions`,
    accessToken,
    body,
    fetchImpl,
  );
  const refused = await refusal(response);
  if (refused) return refused;
  const failure = failed(response, 201);
  if (failure) return failure;
  return { state: "decided" };
}

export async function postApproval(
  baseUrl: string | undefined,
  accessToken: string,
  tenantId: string,
  workspaceId: string,
  documentId: string,
  input: ApprovalInput,
  fetchImpl: typeof fetch = fetch,
): Promise<ApprovalResult> {
  if (![tenantId, workspaceId, documentId].every(isCanonicalUuid)) {
    return { state: "not_found" };
  }
  const response = await post(
    baseUrl,
    `${reviewPath(tenantId, workspaceId, documentId)}/approvals`,
    accessToken,
    {
      run_id: input.runId,
      expected_seq: input.expectedSeq,
      expected_version: input.expectedVersion,
    },
    fetchImpl,
  );
  if (response?.status === 422) {
    const body = (await json(response.clone())) as Record<
      string,
      unknown
    > | null;
    if (body?.detail === "unresolved_fields") {
      const fields = Array.isArray(body.fields) ? body.fields : null;
      return fields && fields.every(isFieldKey)
        ? { state: "unresolved", fields }
        : { state: "unavailable" };
    }
  }
  const refused = await refusal(response);
  if (refused) return refused;
  const failure = failed(response, 201);
  if (failure) return failure;
  const version = (
    (await json(response as Response)) as { version?: unknown } | null
  )?.version;
  return isCount(version) && version >= 1
    ? { state: "approved", version }
    : { state: "unavailable" };
}

export async function exportApproval(
  baseUrl: string | undefined,
  accessToken: string,
  tenantId: string,
  workspaceId: string,
  documentId: string,
  version: number,
  fetchImpl: typeof fetch = fetch,
): Promise<ExportResult> {
  if (
    ![tenantId, workspaceId, documentId].every(isCanonicalUuid) ||
    !isCount(version) ||
    version < 1
  ) {
    return { state: "not_found" };
  }
  const response = await call(
    baseUrl,
    `${reviewPath(tenantId, workspaceId, documentId)}/approvals/${version}/export`,
    accessToken,
    fetchImpl,
    { headersOnly: true },
  );
  const notClean = await notCleanState(response);
  if (notClean) return notClean;
  const failure = failed(response, 200);
  if (failure) return failure;
  return { state: "ok", response: response as Response };
}

function reviewPath(tenantId: string, workspaceId: string, documentId: string) {
  return `${documentsPath(tenantId, workspaceId)}/${documentId}/review`;
}

function post(
  baseUrl: string | undefined,
  path: string,
  accessToken: string,
  body: Record<string, unknown>,
  fetchImpl: typeof fetch,
): Promise<Response | null> {
  return call(baseUrl, path, accessToken, fetchImpl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: new TextEncoder().encode(JSON.stringify(body)),
  });
}

async function notCleanState(
  response: Response | null,
): Promise<NotClean | null> {
  if (response?.status !== 409) return null;
  const reason = await detail(response.clone());
  if (reason === "not_scanned") return { state: "not_scanned" };
  if (reason === "blocked") return { state: "blocked" };
  return null;
}

async function refusal(
  response: Response | null,
): Promise<WriteRefusal | NotClean | null> {
  if (response === null) return null;
  if (response.status === 403) {
    return (await detail(response.clone())) === "not_reviewer"
      ? { state: "not_reviewer" }
      : null;
  }
  if (response.status === 409) {
    const reason = await detail(response.clone());
    if (reason === "stale") return { state: "stale" };
    if (reason === "run_changed") return { state: "run_changed" };
    if (reason === "not_scanned") return { state: "not_scanned" };
    if (reason === "blocked") return { state: "blocked" };
    return null;
  }
  if (response.status === 422) {
    const reason = await detail(response.clone());
    return reason === "invalid_decision" || reason === "invalid_request"
      ? { state: "invalid" }
      : null;
  }
  return null;
}

function parseReview(value: unknown): ReviewState | null {
  const body = record(value);
  if (
    !body ||
    !(body.run_id === null || isUuid(body.run_id)) ||
    typeof body.can_review !== "boolean" ||
    !isCount(body.decisions_through) ||
    !Array.isArray(body.decisions) ||
    !Array.isArray(body.approvals)
  ) {
    return null;
  }
  const decisions: Decision[] = [];
  for (const item of body.decisions) {
    const decision = parseDecision(item);
    if (!decision || decision.seq !== decisions.length + 1) return null;
    decisions.push(decision);
  }
  if (body.decisions_through !== decisions.length) return null;
  const approvals: Approval[] = [];
  for (const item of body.approvals) {
    const approval = parseApproval(item);
    if (!approval || approval.version !== approvals.length + 1) return null;
    approvals.push(approval);
  }
  return {
    runId: body.run_id,
    canReview: body.can_review,
    decisionsThrough: body.decisions_through,
    decisions,
    approvals,
  };
}

function parseDecision(value: unknown): Decision | null {
  const item = record(value);
  if (
    !item ||
    !isCount(item.seq) ||
    !isFieldKey(item.field_key) ||
    (item.resolution !== "candidate" && item.resolution !== "unknown") ||
    !isTime(item.decided_at)
  ) {
    return null;
  }
  const ordinal = item.candidate_ordinal;
  const chosen = item.resolution === "candidate";
  if (chosen ? !isCount(ordinal) : ordinal !== null) return null;
  return {
    seq: item.seq,
    fieldKey: item.field_key,
    resolution: item.resolution,
    candidateOrdinal: chosen ? (ordinal as number) : null,
    decidedAt: item.decided_at,
  };
}

function parseApproval(value: unknown): Approval | null {
  const item = record(value);
  if (
    !item ||
    !isCount(item.version) ||
    !isUuid(item.run_id) ||
    !isCount(item.decisions_through) ||
    !isTime(item.approved_at) ||
    typeof item.record_sha256 !== "string" ||
    !/^[0-9a-f]{64}$/.test(item.record_sha256)
  ) {
    return null;
  }
  return {
    version: item.version,
    runId: item.run_id,
    decisionsThrough: item.decisions_through,
    approvedAt: item.approved_at,
    recordSha256: item.record_sha256,
  };
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isUuid(value: unknown): value is string {
  return typeof value === "string" && isCanonicalUuid(value);
}

function isTime(value: unknown): value is string {
  return typeof value === "string" && !Number.isNaN(Date.parse(value));
}

function isFieldKey(value: unknown): value is FieldKey {
  return (FIELD_KEYS as readonly unknown[]).includes(value);
}
