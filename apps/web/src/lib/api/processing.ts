// Document processing endpoints of the API (DI-1), called server-side with the
// session's access token. The API authorizes every call and only serves
// results while the document's latest malware verdict is clean. Responses are
// parsed strictly: anything that does not match the service_agreement.v1
// contract makes the whole answer "unavailable" rather than rendering a guess.

import { isCanonicalUuid } from "../ids.ts";
import {
  call,
  detail,
  documentsPath,
  failed,
  json,
  type Failure,
} from "./documents.ts";

export type RunStatus =
  "queued" | "running" | "completed" | "needs_review" | "failed";

export interface ProcessingRun {
  id: string;
  status: RunStatus;
  failureCode: string | null;
  attempts: number;
  pipelineVersion: string;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
}

export const FIELD_KEYS = [
  "provider_name",
  "customer_name",
  "effective_date",
  "end_date",
  "governing_law",
  "contract_value",
  "notice_period_days",
] as const;
export type FieldKey = (typeof FIELD_KEYS)[number];

export interface Money {
  amount: string;
  currency: string;
}
export type FieldValue = string | number | Money;

export interface Evidence {
  page: number;
  quote: string;
}

export interface Candidate {
  /** Null for an unparseable or ambiguous mention. */
  value: FieldValue | null;
  /** The text the value was read from; it lies inside one of the quotes. */
  raw: string;
  evidence: Evidence[];
}

export type FieldStatus = "found" | "unknown" | "conflict";
export type UnknownReason = "not_found" | "unparseable" | "ambiguous";

export interface Field {
  status: FieldStatus;
  reason: UnknownReason | null;
  candidates: Candidate[];
}

export interface ExtractionResult {
  outcome: "completed" | "needs_review";
  extractor: string;
  fields: Record<FieldKey, Field>;
}

export type ProcessingResult =
  | { state: "ok"; run: ProcessingRun | null; result: ExtractionResult | null }
  // The document's latest malware verdict is not clean (409).
  | { state: "not_scanned" }
  | { state: "blocked" }
  | Failure;

export type StartResult =
  | { state: "started"; created: boolean; run: ProcessingRun }
  | { state: "not_scanned" }
  | { state: "blocked" }
  // The API has no parser configured (503 processing_unavailable).
  | { state: "processing_unavailable" }
  | Failure;

const STATUSES: readonly RunStatus[] = [
  "queued",
  "running",
  "completed",
  "needs_review",
  "failed",
];
const DATE_FIELDS: readonly FieldKey[] = ["effective_date", "end_date"];
const TEXT_FIELDS: readonly FieldKey[] = [
  "provider_name",
  "customer_name",
  "governing_law",
];

export async function startProcessing(
  baseUrl: string | undefined,
  accessToken: string,
  tenantId: string,
  workspaceId: string,
  documentId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<StartResult> {
  if (![tenantId, workspaceId, documentId].every(isCanonicalUuid)) {
    return { state: "not_found" };
  }
  const response = await call(
    baseUrl,
    processingPath(tenantId, workspaceId, documentId),
    accessToken,
    fetchImpl,
    { method: "POST" },
  );
  if (response?.status === 409) return notClean(response);
  if (
    response?.status === 503 &&
    (await detail(response)) === "processing_unavailable"
  ) {
    return { state: "processing_unavailable" };
  }
  if (response?.status === 200 || response?.status === 202) {
    const body = (await json(response)) as { run?: unknown } | null;
    const run = parseRun(body?.run);
    return run
      ? { state: "started", created: response.status === 202, run }
      : { state: "unavailable" };
  }
  return failed(response, 202) ?? { state: "unavailable" };
}

export async function getProcessing(
  baseUrl: string | undefined,
  accessToken: string,
  tenantId: string,
  workspaceId: string,
  documentId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<ProcessingResult> {
  if (![tenantId, workspaceId, documentId].every(isCanonicalUuid)) {
    return { state: "not_found" };
  }
  const response = await call(
    baseUrl,
    processingPath(tenantId, workspaceId, documentId),
    accessToken,
    fetchImpl,
  );
  if (response?.status === 409) return notClean(response);
  const failure = failed(response, 200);
  if (failure) return failure;
  const body = (await json(response as Response)) as {
    run?: unknown;
    result?: unknown;
  } | null;
  if (body === null || typeof body !== "object")
    return { state: "unavailable" };
  if (body.run === null) {
    return body.result === null
      ? { state: "ok", run: null, result: null }
      : { state: "unavailable" };
  }
  const run = parseRun(body.run);
  if (!run) return { state: "unavailable" };
  const finished = run.status === "completed" || run.status === "needs_review";
  if (!finished) {
    return body.result === null
      ? { state: "ok", run, result: null }
      : { state: "unavailable" };
  }
  const result = parseResult(body.result);
  if (!result || result.outcome !== run.status) return { state: "unavailable" };
  return { state: "ok", run, result };
}

function processingPath(
  tenantId: string,
  workspaceId: string,
  documentId: string,
): string {
  return `${documentsPath(tenantId, workspaceId)}/${documentId}/processing`;
}

async function notClean(
  response: Response,
): Promise<{ state: "not_scanned" } | { state: "blocked" }> {
  return (await detail(response)) === "not_scanned"
    ? { state: "not_scanned" }
    : { state: "blocked" };
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function isTime(value: unknown): value is string {
  return typeof value === "string" && !Number.isNaN(Date.parse(value));
}

function parseRun(value: unknown): ProcessingRun | null {
  const run = record(value);
  if (!run) return null;
  const {
    id,
    status,
    failure_code: failureCode,
    attempts,
    pipeline_version: pipelineVersion,
    created_at: createdAt,
    started_at: startedAt,
    finished_at: finishedAt,
  } = run;
  if (
    !isCanonicalUuid(id) ||
    !STATUSES.includes(status as RunStatus) ||
    !(failureCode === null || typeof failureCode === "string") ||
    (status === "failed") !== (failureCode !== null) ||
    typeof attempts !== "number" ||
    !Number.isSafeInteger(attempts) ||
    attempts < 0 ||
    typeof pipelineVersion !== "string" ||
    !isTime(createdAt) ||
    !(startedAt === null || isTime(startedAt)) ||
    !(finishedAt === null || isTime(finishedAt))
  ) {
    return null;
  }
  return {
    id,
    status: status as RunStatus,
    failureCode,
    attempts,
    pipelineVersion,
    createdAt,
    startedAt,
    finishedAt,
  };
}

function parseResult(value: unknown): ExtractionResult | null {
  const result = record(value);
  const fields = record(result?.fields);
  if (
    !result ||
    !fields ||
    result.schema !== "service_agreement.v1" ||
    (result.outcome !== "completed" && result.outcome !== "needs_review") ||
    typeof result.extractor !== "string" ||
    Object.keys(fields).length !== FIELD_KEYS.length
  ) {
    return null;
  }
  const parsed: Partial<Record<FieldKey, Field>> = {};
  for (const key of FIELD_KEYS) {
    const field = parseField(key, fields[key]);
    if (!field) return null;
    parsed[key] = field;
  }
  return {
    outcome: result.outcome,
    extractor: result.extractor,
    fields: parsed as Record<FieldKey, Field>,
  };
}

function parseField(key: FieldKey, value: unknown): Field | null {
  const field = record(value);
  if (!field || !Array.isArray(field.candidates)) return null;
  const { status, reason } = field;
  if (status !== "found" && status !== "unknown" && status !== "conflict") {
    return null;
  }
  if (
    !(
      reason === null ||
      reason === "not_found" ||
      reason === "unparseable" ||
      reason === "ambiguous"
    ) ||
    (status === "unknown") !== (reason !== null)
  ) {
    return null;
  }
  const candidates: Candidate[] = [];
  for (const item of field.candidates) {
    const candidate = parseCandidate(key, item);
    if (!candidate) return null;
    candidates.push(candidate);
  }
  const values = candidates.filter((c) => c.value !== null).length;
  const consistent =
    status === "found"
      ? candidates.length === 1 && values === 1
      : status === "conflict"
        ? candidates.length >= 2 && values >= 1
        : reason === "not_found"
          ? candidates.length === 0
          : candidates.length >= 1 && values === 0;
  return consistent ? { status, reason, candidates } : null;
}

function parseCandidate(key: FieldKey, value: unknown): Candidate | null {
  const candidate = record(value);
  if (
    !candidate ||
    typeof candidate.raw !== "string" ||
    candidate.raw.length === 0 ||
    !Array.isArray(candidate.evidence) ||
    candidate.evidence.length === 0
  ) {
    return null;
  }
  const fieldValue =
    candidate.value === null ? null : parseValue(key, candidate.value);
  if (fieldValue === undefined) return null;
  const evidence: Evidence[] = [];
  for (const item of candidate.evidence) {
    const span = record(item);
    if (
      !span ||
      typeof span.page !== "number" ||
      !Number.isSafeInteger(span.page) ||
      span.page < 1 ||
      typeof span.quote !== "string" ||
      span.quote.length === 0 ||
      typeof span.start !== "number" ||
      typeof span.end !== "number" ||
      span.end - span.start !== span.quote.length
    ) {
      return null;
    }
    evidence.push({ page: span.page, quote: span.quote });
  }
  // The contract: the raw value lies inside one of its quotes.
  if (!evidence.some((span) => span.quote.includes(candidate.raw as string))) {
    return null;
  }
  return { value: fieldValue, raw: candidate.raw, evidence };
}

/** The typed value, or undefined when it does not fit the field. */
function parseValue(key: FieldKey, value: unknown): FieldValue | undefined {
  if (DATE_FIELDS.includes(key)) {
    return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)
      ? value
      : undefined;
  }
  if (TEXT_FIELDS.includes(key)) {
    return typeof value === "string" && value.length > 0 ? value : undefined;
  }
  if (key === "notice_period_days") {
    return typeof value === "number" &&
      Number.isSafeInteger(value) &&
      value >= 0
      ? value
      : undefined;
  }
  const money = record(value);
  return money &&
    typeof money.amount === "string" &&
    /^\d{1,15}\.\d{2}$/.test(money.amount) &&
    typeof money.currency === "string" &&
    /^[A-Z]{3}$/.test(money.currency)
    ? { amount: money.amount, currency: money.currency }
    : undefined;
}
