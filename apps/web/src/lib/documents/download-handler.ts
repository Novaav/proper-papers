// GET /tenants/{t}/workspaces/{w}/documents/{d}/download: streams the original
// bytes from the API to the browser. The session's token stays on the server;
// the API authorizes every download (token, membership, row-level security).
// The file is always an attachment and never rendered: the API's
// Content-Disposition is passed through, with nosniff and a sandbox policy.

import type { DownloadResult } from "../api/documents.ts";
import { reauthenticateOnUnauthorized } from "../auth/redirect.ts";
import type { Session } from "../auth/session.ts";
import { isCanonicalUuid } from "../ids.ts";

export interface DownloadDeps {
  readSession: () => Promise<Session | null>;
  download: (
    accessToken: string,
    tenantId: string,
    workspaceId: string,
    documentId: string,
  ) => Promise<DownloadResult>;
  /** Epoch seconds. */
  now: () => number;
  log: (message: string) => void;
}

const NO_STORE = { "Cache-Control": "no-store" };

export async function handleDownload(
  tenantId: string,
  workspaceId: string,
  documentId: string,
  deps: DownloadDeps,
): Promise<Response> {
  if (![tenantId, workspaceId, documentId].every(isCanonicalUuid)) {
    return text(404, "Not found.");
  }
  const pagePath = `/tenants/${tenantId}/workspaces/${workspaceId}`;
  const login = () =>
    redirect(`/auth/login?returnTo=${encodeURIComponent(pagePath)}`);

  const session = await deps.readSession();
  if (!session) return login();

  const result = await deps.download(
    session.accessToken,
    tenantId,
    workspaceId,
    documentId,
  );
  switch (result.state) {
    case "ok":
      return attachment(result.response);
    case "not_found":
      return text(404, "Not found.");
    case "unauthorized":
      if (reauthenticateOnUnauthorized(session, deps.now())) return login();
      return redirect(`${pagePath}?error=rejected`);
    case "not_scanned":
    case "blocked":
      // Listed but not clean: back to the page with a fixed message.
      return redirect(`${pagePath}?error=${result.state}`);
    case "unavailable":
      deps.log("download failed: api_unavailable");
      return text(503, "The document is unavailable right now.");
  }
}

function attachment(upstream: Response): Response {
  const disposition = upstream.headers.get("content-disposition") ?? "";
  // The API always sends an attachment; anything else is refused rather than
  // risk the browser rendering the file.
  if (!disposition.toLowerCase().startsWith("attachment")) {
    return text(502, "The document could not be downloaded.");
  }
  const headers: Record<string, string> = {
    ...NO_STORE,
    "Content-Type": "application/pdf",
    "Content-Disposition": disposition,
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
  };
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
