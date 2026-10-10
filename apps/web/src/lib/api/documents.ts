// Workspace and document endpoints of the API, called server-side with the
// session's access token. The API authorizes every call (membership, row-level
// security) and validates uploads (size, PDF signature, name); this module
// relays and validates responses. IDs are checked before they reach a URL.

import { isCanonicalUuid } from "../ids.ts";
import { parseWorkspace, type Workspace } from "./workspaces.ts";

/** The API's malware-scan status of a version; only "clean" can be downloaded. */
export type ScanStatus = "clean" | "not_scanned" | "blocked";

export interface DocumentSummary {
  id: string;
  name: string;
  createdAt: string;
  sizeBytes: number;
  sha256: string;
  scanStatus: ScanStatus;
}

export type Failure =
  { state: "not_found" } | { state: "unauthorized" } | { state: "unavailable" };

export type WorkspaceResult = { state: "ok"; workspace: Workspace } | Failure;
export type DocumentListResult =
  { state: "ok"; documents: DocumentSummary[] } | Failure;
export type DocumentResult =
  { state: "ok"; document: DocumentSummary } | Failure;
export type UploadResult =
  | { state: "uploaded"; document: DocumentSummary }
  | { state: "too_large" }
  | { state: "not_pdf" }
  | { state: "invalid_name" }
  // Malware scanning (the API decides; nothing was stored).
  | { state: "encrypted_pdf" }
  | { state: "malware_detected" }
  | { state: "scan_incomplete" }
  | { state: "scan_unavailable" }
  // A 422 with a reason this client does not know.
  | { state: "upload_rejected" }
  | Failure;
/** On success the API's response is passed through unread (streamed). */
export type DownloadResult =
  | { state: "ok"; response: Response }
  // The version exists but is not clean (409): never scanned, or blocked.
  | { state: "not_scanned" }
  | { state: "blocked" }
  | Failure;

// The API's 422 reasons for an upload, each mapped to its own state.
const UPLOAD_REJECTIONS = [
  "invalid_name",
  "encrypted_pdf",
  "malware_detected",
  "scan_incomplete",
] as const;

const TIMEOUT_MS = 5000;
// An upload of up to 20 MiB may take longer than a metadata call.
const UPLOAD_TIMEOUT_MS = 60_000;

export async function getWorkspace(
  baseUrl: string | undefined,
  accessToken: string,
  tenantId: string,
  workspaceId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<WorkspaceResult> {
  if (!isCanonicalUuid(tenantId) || !isCanonicalUuid(workspaceId)) {
    return { state: "not_found" };
  }
  const response = await call(
    baseUrl,
    `/v1/tenants/${tenantId}/workspaces/${workspaceId}`,
    accessToken,
    fetchImpl,
  );
  const failure = failed(response, 200);
  if (failure) return failure;
  const workspace = parseWorkspace(await json(response as Response));
  return workspace ? { state: "ok", workspace } : { state: "unavailable" };
}

export async function listDocuments(
  baseUrl: string | undefined,
  accessToken: string,
  tenantId: string,
  workspaceId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<DocumentListResult> {
  if (!isCanonicalUuid(tenantId) || !isCanonicalUuid(workspaceId)) {
    return { state: "not_found" };
  }
  const response = await call(
    baseUrl,
    documentsPath(tenantId, workspaceId),
    accessToken,
    fetchImpl,
  );
  const failure = failed(response, 200);
  if (failure) return failure;
  const items = (
    (await json(response as Response)) as { documents?: unknown } | null
  )?.documents;
  if (!Array.isArray(items)) return { state: "unavailable" };
  const documents: DocumentSummary[] = [];
  for (const item of items) {
    const document = parseDocument(item);
    if (!document) return { state: "unavailable" };
    documents.push(document);
  }
  return { state: "ok", documents };
}

/** One document's metadata; not_found also for other tenants and malformed IDs. */
export async function getDocument(
  baseUrl: string | undefined,
  accessToken: string,
  tenantId: string,
  workspaceId: string,
  documentId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<DocumentResult> {
  if (![tenantId, workspaceId, documentId].every(isCanonicalUuid)) {
    return { state: "not_found" };
  }
  const response = await call(
    baseUrl,
    `${documentsPath(tenantId, workspaceId)}/${documentId}`,
    accessToken,
    fetchImpl,
  );
  const failure = failed(response, 200);
  if (failure) return failure;
  const document = parseDocument(await json(response as Response));
  return document ? { state: "ok", document } : { state: "unavailable" };
}

export async function uploadDocument(
  baseUrl: string | undefined,
  accessToken: string,
  tenantId: string,
  workspaceId: string,
  file: { bytes: Uint8Array<ArrayBuffer>; name: string; type: string },
  fetchImpl: typeof fetch = fetch,
): Promise<UploadResult> {
  if (!isCanonicalUuid(tenantId) || !isCanonicalUuid(workspaceId)) {
    return { state: "not_found" };
  }
  // The browser's type and name are forwarded as hints; the API checks the
  // bytes and normalises the name itself.
  const response = await call(
    baseUrl,
    documentsPath(tenantId, workspaceId),
    accessToken,
    fetchImpl,
    {
      method: "POST",
      headers: {
        "Content-Type": file.type || "application/octet-stream",
        "X-Document-Name": encodeURIComponent(file.name),
      },
      body: file.bytes,
      timeoutMs: UPLOAD_TIMEOUT_MS,
    },
  );
  if (response?.status === 413) return { state: "too_large" };
  if (response?.status === 415) return { state: "not_pdf" };
  if (response?.status === 422) {
    const reason = await detail(response);
    const known = UPLOAD_REJECTIONS.find((value) => value === reason);
    return known ? { state: known } : { state: "upload_rejected" };
  }
  if (
    response?.status === 503 &&
    (await detail(response)) === "scan_unavailable"
  ) {
    return { state: "scan_unavailable" };
  }
  const failure = failed(response, 201);
  if (failure) return failure;
  const document = parseDocument(await json(response as Response));
  return document ? { state: "uploaded", document } : { state: "unavailable" };
}

export async function downloadDocument(
  baseUrl: string | undefined,
  accessToken: string,
  tenantId: string,
  workspaceId: string,
  documentId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<DownloadResult> {
  if (![tenantId, workspaceId, documentId].every(isCanonicalUuid)) {
    return { state: "not_found" };
  }
  const response = await call(
    baseUrl,
    `${documentsPath(tenantId, workspaceId)}/${documentId}/content`,
    accessToken,
    fetchImpl,
    // The body is streamed to the browser at the browser's pace: time out
    // waiting for the response, not while streaming it.
    { timeoutMs: TIMEOUT_MS, headersOnly: true },
  );
  if (response?.status === 409) {
    return (await detail(response)) === "not_scanned"
      ? { state: "not_scanned" }
      : { state: "blocked" };
  }
  const failure = failed(response, 200);
  if (failure) return failure;
  return { state: "ok", response: response as Response };
}

export function documentsPath(tenantId: string, workspaceId: string): string {
  return `/v1/tenants/${tenantId}/workspaces/${workspaceId}/documents`;
}

// The helpers below are shared with the processing client (./processing.ts).

export function failed(
  response: Response | null,
  expected: number,
): Failure | null {
  if (response === null) return { state: "unavailable" };
  if (response.status === expected) return null;
  if (response.status === 404) return { state: "not_found" };
  if (response.status === 401) return { state: "unauthorized" };
  return { state: "unavailable" };
}

export async function call(
  baseUrl: string | undefined,
  path: string,
  accessToken: string,
  fetchImpl: typeof fetch,
  options: {
    method?: string;
    headers?: Record<string, string>;
    body?: Uint8Array<ArrayBuffer>;
    timeoutMs?: number;
    /** Stop the timeout once the response headers arrive. */
    headersOnly?: boolean;
  } = {},
): Promise<Response | null> {
  if (!baseUrl) return null;
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(),
    options.timeoutMs ?? TIMEOUT_MS,
  );
  // Like AbortSignal.timeout: a pending timer does not keep the process alive.
  timer.unref?.();
  try {
    return await fetchImpl(new URL(path, baseUrl), {
      method: options.method ?? "GET",
      headers: { Authorization: `Bearer ${accessToken}`, ...options.headers },
      ...(options.body ? { body: options.body } : {}),
      cache: "no-store",
      redirect: "error",
      signal: controller.signal,
    });
  } catch {
    return null;
  } finally {
    if (options.headersOnly) clearTimeout(timer);
  }
}

/** The `detail` of an API error body, or null. */
export async function detail(response: Response): Promise<string | null> {
  const body = (await json(response)) as { detail?: unknown } | null;
  return typeof body?.detail === "string" ? body.detail : null;
}

export async function json(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function parseDocument(value: unknown): DocumentSummary | null {
  if (typeof value !== "object" || value === null) return null;
  const {
    id,
    name,
    created_at: createdAt,
    version,
  } = value as Record<string, unknown>;
  const {
    size_bytes: sizeBytes,
    sha256,
    scan_status: scanStatus,
  } = (version ?? {}) as Record<string, unknown>;
  if (
    !isCanonicalUuid(id) ||
    typeof name !== "string" ||
    typeof createdAt !== "string" ||
    Number.isNaN(Date.parse(createdAt)) ||
    typeof sizeBytes !== "number" ||
    !Number.isSafeInteger(sizeBytes) ||
    typeof sha256 !== "string" ||
    !/^[0-9a-f]{64}$/.test(sha256) ||
    (scanStatus !== "clean" &&
      scanStatus !== "not_scanned" &&
      scanStatus !== "blocked")
  ) {
    return null;
  }
  return { id, name, createdAt, sizeBytes, sha256, scanStatus };
}
