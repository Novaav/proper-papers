// POST /tenants/{t}/workspaces/{w}/documents/upload: the upload form's target.
//
// This route is excluded from src/proxy.ts, whose request-body buffering would
// silently truncate bodies over 10 MB. Order of checks: configuration, exact
// Origin (CSRF), ID format, session, membership and workspace (asked of the
// API before any of the body is read), content type, body size (declared, then
// counted while reading), one file field. The PDF signature and the name are
// decided by the API, which checks membership again. The answer is always a redirect back to
// the workspace page with a fixed code (post/redirect/get) or a plain-text
// error; submitted names and bytes are never echoed.

import type { UploadResult, WorkspaceResult } from "../api/documents.ts";
import { reauthenticateOnUnauthorized } from "../auth/redirect.ts";
import type { Session } from "../auth/session.ts";
import { isCanonicalUuid } from "../ids.ts";

/** The API's per-file limit (20 MiB). */
export const DOCUMENT_MAX_BYTES = 20 * 1024 * 1024;
/** Room for the multipart boundaries and part headers around one file. */
export const MULTIPART_OVERHEAD_BYTES = 64 * 1024;
export const MAX_UPLOAD_BODY_BYTES =
  DOCUMENT_MAX_BYTES + MULTIPART_OVERHEAD_BYTES;

export interface UploadDeps {
  /** APP_ORIGIN, or null when authentication is not configured. */
  appOrigin: string | null;
  readSession: () => Promise<Session | null>;
  upload: (
    accessToken: string,
    tenantId: string,
    workspaceId: string,
    file: { bytes: Uint8Array<ArrayBuffer>; name: string; type: string },
  ) => Promise<UploadResult>;
  /** The workspace as the caller sees it (membership check); no body is sent. */
  workspace: (
    accessToken: string,
    tenantId: string,
    workspaceId: string,
  ) => Promise<WorkspaceResult>;
  /** Epoch seconds. */
  now: () => number;
  /** Receives a fixed category only: never names, bytes or tokens. */
  log: (message: string) => void;
}

const NO_STORE = { "Cache-Control": "no-store" };

export async function handleUpload(
  request: Request,
  tenantId: string,
  workspaceId: string,
  deps: UploadDeps,
): Promise<Response> {
  const { appOrigin } = deps;
  if (!appOrigin) return text(503, "Service unavailable.");
  if (request.headers.get("origin") !== appOrigin) {
    deps.log("upload refused: origin_mismatch");
    return text(403, "Forbidden.");
  }
  if (!isCanonicalUuid(tenantId) || !isCanonicalUuid(workspaceId)) {
    return text(404, "Not found.");
  }

  const pagePath = `/tenants/${tenantId}/workspaces/${workspaceId}`;
  const back = (query: string) => redirect(`${appOrigin}${pagePath}?${query}`);
  const login = () =>
    redirect(
      `${appOrigin}/auth/login?returnTo=${encodeURIComponent(pagePath)}`,
    );

  const session = await deps.readSession();
  if (!session) return login();

  // A non-member (or an unknown workspace) is refused before this server reads
  // any of the body (D-058).
  const target = await deps.workspace(
    session.accessToken,
    tenantId,
    workspaceId,
  );
  if (target.state !== "ok") return refused(target.state, session);

  const file = await readSingleFile(request);
  if (file === "bad_request") return text(400, "Bad request.");
  if (file === "too_large") return back("error=too_large");
  if (file === "missing") return back("error=missing_file");

  const result = await deps.upload(
    session.accessToken,
    tenantId,
    workspaceId,
    file,
  );
  switch (result.state) {
    case "uploaded":
      return back("uploaded=1");
    case "too_large":
      return back("error=too_large");
    case "not_pdf":
      return back("error=not_pdf");
    case "invalid_name":
      return back("error=invalid_name");
    // Malware scanning, decided by the API: nothing was stored.
    case "encrypted_pdf":
    case "malware_detected":
    case "scan_incomplete":
    case "scan_unavailable":
    case "upload_rejected":
      return back(`error=${result.state}`);
    default:
      return refused(result.state, session);
  }

  function refused(
    state: "not_found" | "unauthorized" | "unavailable",
    session: Session,
  ): Response {
    switch (state) {
      case "not_found":
        // The page renders the uniform "not found or no access" page.
        return redirect(`${appOrigin}${pagePath}`);
      case "unauthorized":
        if (reauthenticateOnUnauthorized(session, deps.now())) return login();
        return back("error=rejected");
      case "unavailable":
        deps.log("upload failed: api_unavailable");
        return back("error=unavailable");
    }
  }
}

type FileResult =
  | { bytes: Uint8Array<ArrayBuffer>; name: string; type: string }
  | "too_large"
  | "missing"
  | "bad_request";

/** Reads a multipart/form-data body of at most MAX_UPLOAD_BODY_BYTES with exactly one file. */
async function readSingleFile(request: Request): Promise<FileResult> {
  const contentType = request.headers.get("content-type") ?? "";
  if (
    !contentType.toLowerCase().startsWith("multipart/form-data;") ||
    !request.body
  ) {
    return "bad_request";
  }
  const declared = request.headers.get("content-length");
  if (
    declared &&
    /^\d+$/.test(declared) &&
    Number(declared) > MAX_UPLOAD_BODY_BYTES
  ) {
    return "too_large";
  }
  const chunks: Uint8Array[] = [];
  let size = 0;
  const reader = request.body.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_UPLOAD_BODY_BYTES) {
      await reader.cancel();
      return "too_large";
    }
    chunks.push(value);
  }
  let form: FormData;
  try {
    form = await new Response(Buffer.concat(chunks), {
      headers: { "Content-Type": contentType },
    }).formData();
  } catch {
    return "bad_request";
  }
  const files = form.getAll("file");
  if (files.length !== 1) return files.length === 0 ? "missing" : "bad_request";
  const file = files[0];
  if (!(file instanceof File) || file.size === 0) return "missing";
  if (file.size > DOCUMENT_MAX_BYTES) return "too_large";
  return {
    bytes: new Uint8Array(await file.arrayBuffer()),
    name: file.name,
    type: file.type,
  };
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
