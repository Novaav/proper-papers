// The review routes of the document page (R1):
//   POST …/documents/{d}/review/decisions           (a decision form)
//   POST …/documents/{d}/review/approvals           (the approve form)
//   GET  …/documents/{d}/review/approvals/{v}/export (download an approved record)
//
// The forms are plain HTML forms, so review works without JavaScript. Order of
// checks for a write: configuration, exact Origin (CSRF), ID format, session,
// form. The API decides the role, numbering, staleness and validity on every
// call. A write always answers with a redirect (post/redirect/get) to the
// document page with a fixed code and the field's anchor; request input is
// never echoed.

import type { FieldKey } from "../api/processing.ts";
import { FIELD_KEYS } from "../api/processing.ts";
import type {
  ApprovalInput,
  ApprovalResult,
  DecisionInput,
  DecisionResult,
  ExportResult,
} from "../api/review.ts";
import { reauthenticateOnUnauthorized } from "../auth/redirect.ts";
import type { Session } from "../auth/session.ts";
import { isCanonicalUuid } from "../ids.ts";

/** The codes the page turns into fixed messages (?review=…). */
export type ReviewCode =
  | "decided"
  | "approved"
  | "stale"
  | "run_changed"
  | "invalid"
  | "unresolved"
  | "not_reviewer"
  | "not_clean"
  | "rejected"
  | "unavailable";

interface WriteDeps {
  /** APP_ORIGIN, or null when authentication is not configured. */
  appOrigin: string | null;
  readSession: () => Promise<Session | null>;
  /** Epoch seconds. */
  now: () => number;
  /** Receives a fixed category, never IDs, form values or tokens. */
  log: (message: string) => void;
}

export interface DecisionDeps extends WriteDeps {
  decide: (
    accessToken: string,
    tenantId: string,
    workspaceId: string,
    documentId: string,
    input: DecisionInput,
  ) => Promise<DecisionResult>;
}

export interface ApprovalDeps extends WriteDeps {
  approve: (
    accessToken: string,
    tenantId: string,
    workspaceId: string,
    documentId: string,
    input: ApprovalInput,
  ) => Promise<ApprovalResult>;
}

export interface ExportDeps {
  readSession: () => Promise<Session | null>;
  export: (
    accessToken: string,
    tenantId: string,
    workspaceId: string,
    documentId: string,
    version: number,
  ) => Promise<ExportResult>;
  now: () => number;
  log: (message: string) => void;
}

const NO_STORE = { "Cache-Control": "no-store" };
// A review form is a handful of short fields.
const MAX_FORM_BYTES = 2048;
const COUNT = /^(0|[1-9]\d{0,8})$/;
const VERSION = /^[1-9]\d{0,8}$/;

export async function handleDecision(
  request: Request,
  tenantId: string,
  workspaceId: string,
  documentId: string,
  deps: DecisionDeps,
): Promise<Response> {
  return write(
    request,
    tenantId,
    workspaceId,
    documentId,
    deps,
    async (form, session) => {
      const input = decisionInput(form);
      if (!input) return { code: "invalid", anchor: null };
      // A refusal stays on the field that was decided; only a saved decision
      // moves on, to where the page said the review goes next.
      const anchor = fieldAnchor(input.fieldKey);
      const result = await deps.decide(
        session.accessToken,
        tenantId,
        workspaceId,
        documentId,
        input,
      );
      if (result.state === "decided") {
        return {
          code: "decided",
          anchor: advanceAnchor(form.get("after")) ?? anchor,
        };
      }
      return { code: refusalCode(result, deps), anchor };
    },
  );
}

export async function handleApproval(
  request: Request,
  tenantId: string,
  workspaceId: string,
  documentId: string,
  deps: ApprovalDeps,
): Promise<Response> {
  return write(
    request,
    tenantId,
    workspaceId,
    documentId,
    deps,
    async (form, session) => {
      const input = approvalInput(form);
      if (!input) return { code: "invalid", anchor: "approval" };
      const result = await deps.approve(
        session.accessToken,
        tenantId,
        workspaceId,
        documentId,
        input,
      );
      if (result.state === "approved")
        return { code: "approved", anchor: "approval" };
      if (result.state === "unresolved")
        return { code: "unresolved", anchor: "approval" };
      return { code: refusalCode(result, deps), anchor: "approval" };
    },
  );
}

/** Streams one approved record (JSON) from the API; always an attachment. */
export async function handleExport(
  tenantId: string,
  workspaceId: string,
  documentId: string,
  version: string,
  deps: ExportDeps,
): Promise<Response> {
  if (
    ![tenantId, workspaceId, documentId].every(isCanonicalUuid) ||
    !VERSION.test(version)
  ) {
    return text(404, "Not found.");
  }
  const pagePath = documentPath(tenantId, workspaceId, documentId);
  const login = () =>
    redirect(`/auth/login?returnTo=${encodeURIComponent(pagePath)}`);
  const session = await deps.readSession();
  if (!session) return login();
  const result = await deps.export(
    session.accessToken,
    tenantId,
    workspaceId,
    documentId,
    Number(version),
  );
  switch (result.state) {
    case "ok":
      return attachment(result.response);
    case "not_found":
      return text(404, "Not found.");
    case "unauthorized":
      if (reauthenticateOnUnauthorized(session, deps.now())) return login();
      return redirect(`${pagePath}?review=rejected`);
    case "not_scanned":
    case "blocked":
      return redirect(`${pagePath}?review=not_clean`);
    case "unavailable":
      deps.log("export record failed: api_unavailable");
      return text(503, "The approved record is unavailable right now.");
  }
}

type Outcome = { code: ReviewCode | null; anchor: string | null };

async function write(
  request: Request,
  tenantId: string,
  workspaceId: string,
  documentId: string,
  deps: WriteDeps,
  act: (form: URLSearchParams, session: Session) => Promise<Outcome>,
): Promise<Response> {
  const { appOrigin } = deps;
  if (!appOrigin) return text(503, "Service unavailable.");
  if (request.headers.get("origin") !== appOrigin) {
    deps.log("review write refused: origin_mismatch");
    return text(403, "Forbidden.");
  }
  if (![tenantId, workspaceId, documentId].every(isCanonicalUuid)) {
    return text(404, "Not found.");
  }
  const pagePath = documentPath(tenantId, workspaceId, documentId);
  const session = await deps.readSession();
  if (!session) {
    return redirect(
      `${appOrigin}/auth/login?returnTo=${encodeURIComponent(pagePath)}`,
    );
  }
  const form = await readForm(request);
  const outcome = form
    ? await act(form, session)
    : { code: "invalid" as const, anchor: null };
  if (
    outcome.code === "rejected" &&
    reauthenticateOnUnauthorized(session, deps.now())
  ) {
    return redirect(
      `${appOrigin}/auth/login?returnTo=${encodeURIComponent(pagePath)}`,
    );
  }
  const query = outcome.code ? `?review=${outcome.code}` : "";
  const fragment = outcome.anchor ? `#${outcome.anchor}` : "";
  return redirect(`${appOrigin}${pagePath}${query}${fragment}`);
}

function refusalCode(
  result: Exclude<
    DecisionResult | ApprovalResult,
    { state: "decided" | "approved" | "unresolved" }
  >,
  deps: WriteDeps,
): ReviewCode | null {
  switch (result.state) {
    case "stale":
    case "run_changed":
    case "invalid":
    case "not_reviewer":
      return result.state;
    case "not_scanned":
    case "blocked":
      return "not_clean";
    case "not_found":
      // The document page renders the uniform "not found or no access" page.
      return null;
    case "unauthorized":
      return "rejected";
    case "unavailable":
      deps.log("review write failed: api_unavailable");
      return "unavailable";
  }
}

/** The form as URL-encoded fields, or null if it is not a small, plain form. */
async function readForm(request: Request): Promise<URLSearchParams | null> {
  const type = request.headers.get("content-type") ?? "";
  if (!type.toLowerCase().startsWith("application/x-www-form-urlencoded"))
    return null;
  const declared = request.headers.get("content-length");
  if (
    declared &&
    (!/^\d+$/.test(declared) || Number(declared) > MAX_FORM_BYTES)
  ) {
    return null;
  }
  const body = await request.text();
  if (new TextEncoder().encode(body).length > MAX_FORM_BYTES) return null;
  return new URLSearchParams(body);
}

/** Exactly these fields, each once; anything else makes the form invalid. */
function fields(
  form: URLSearchParams,
  required: readonly string[],
  optional: readonly string[] = [],
): Record<string, string> | null {
  const values: Record<string, string> = {};
  for (const [name, value] of form) {
    if (![...required, ...optional].includes(name) || name in values)
      return null;
    values[name] = value;
  }
  return required.every((name) => name in values) ? values : null;
}

function decisionInput(form: URLSearchParams): DecisionInput | null {
  const values = fields(
    form,
    ["run_id", "expected_seq", "field_key", "resolution"],
    ["candidate_ordinal", "after"],
  );
  if (!values) return null;
  const {
    run_id: runId,
    expected_seq: seq,
    field_key: key,
    resolution,
  } = values;
  const ordinal = values.candidate_ordinal;
  const after = values.after;
  if (
    !runId ||
    !isCanonicalUuid(runId) ||
    !seq ||
    !COUNT.test(seq) ||
    !(FIELD_KEYS as readonly string[]).includes(key ?? "") ||
    (resolution !== "candidate" && resolution !== "unknown") ||
    (resolution === "candidate") !== (ordinal !== undefined) ||
    (ordinal !== undefined && !COUNT.test(ordinal)) ||
    (after !== undefined && !isAfter(after))
  ) {
    return null;
  }
  return {
    runId,
    expectedSeq: Number(seq),
    fieldKey: key as FieldKey,
    resolution,
    candidateOrdinal: ordinal === undefined ? null : Number(ordinal),
  };
}

/** Where the page says the review goes after a saved decision: a field, the approval, or nowhere. */
const AFTER_APPROVAL = "approval";
const AFTER_STAY = "stay";
function isAfter(value: string): boolean {
  return (
    value === AFTER_APPROVAL ||
    value === AFTER_STAY ||
    (FIELD_KEYS as readonly string[]).includes(value)
  );
}

/** The anchor for a saved decision's `after`, or null to stay on the decided field. */
function advanceAnchor(after: string | null): string | null {
  if (after === null || after === AFTER_STAY) return null;
  if (after === AFTER_APPROVAL) return "approval";
  return fieldAnchor(after as FieldKey);
}

function approvalInput(form: URLSearchParams): ApprovalInput | null {
  const values = fields(form, ["run_id", "expected_seq", "expected_version"]);
  if (!values) return null;
  const {
    run_id: runId,
    expected_seq: seq,
    expected_version: version,
  } = values;
  if (
    !runId ||
    !isCanonicalUuid(runId) ||
    !seq ||
    !COUNT.test(seq) ||
    !version ||
    !COUNT.test(version)
  ) {
    return null;
  }
  return { runId, expectedSeq: Number(seq), expectedVersion: Number(version) };
}

export function fieldAnchor(key: FieldKey): string {
  return `field-${key.replaceAll("_", "-")}`;
}

function documentPath(
  tenantId: string,
  workspaceId: string,
  documentId: string,
) {
  return `/tenants/${tenantId}/workspaces/${workspaceId}/documents/${documentId}`;
}

function attachment(upstream: Response): Response {
  const disposition = upstream.headers.get("content-disposition") ?? "";
  // The API always sends an attachment; anything else is refused rather than
  // risk the browser rendering the record.
  if (!disposition.toLowerCase().startsWith("attachment")) {
    return text(502, "The approved record could not be downloaded.");
  }
  const headers: Record<string, string> = {
    ...NO_STORE,
    "Content-Type": "application/json",
    "Content-Disposition": disposition,
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
  };
  const digest = upstream.headers.get("x-record-sha256");
  if (digest && /^[0-9a-f]{64}$/.test(digest))
    headers["X-Record-SHA256"] = digest;
  const length = upstream.headers.get("content-length");
  if (length && /^\d+$/.test(length)) headers["Content-Length"] = length;
  return new Response(upstream.body, { status: 200, headers });
}

function redirect(location: string): Response {
  return new Response(null, {
    status: 303,
    headers: { ...NO_STORE, Location: location },
  });
}

function text(status: number, body: string): Response {
  return new Response(body, {
    status,
    headers: { ...NO_STORE, "Content-Type": "text/plain; charset=utf-8" },
  });
}
