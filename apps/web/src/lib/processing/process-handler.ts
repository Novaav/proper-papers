// POST /tenants/{t}/workspaces/{w}/documents/{d}/process: the "Process document"
// form's target.
//
// Order of checks: configuration, exact Origin (CSRF), ID format, session. The
// API decides membership, the document and its malware verdict on every call;
// starting is idempotent there (an existing run is returned as it is). The
// response is always a redirect (post/redirect/get) to the document page with
// a fixed code, or a plain-text error; request input is never echoed.

import type { StartResult } from "../api/processing.ts";
import { reauthenticateOnUnauthorized } from "../auth/redirect.ts";
import type { Session } from "../auth/session.ts";
import { isCanonicalUuid } from "../ids.ts";

export interface ProcessDeps {
  /** APP_ORIGIN, or null when authentication is not configured. */
  appOrigin: string | null;
  readSession: () => Promise<Session | null>;
  start: (
    accessToken: string,
    tenantId: string,
    workspaceId: string,
    documentId: string,
  ) => Promise<StartResult>;
  /** Epoch seconds. */
  now: () => number;
  /** Receives a fixed category, never IDs of other tenants or tokens. */
  log: (message: string) => void;
}

const NO_STORE = { "Cache-Control": "no-store" };

export async function handleProcess(
  request: Request,
  tenantId: string,
  workspaceId: string,
  documentId: string,
  deps: ProcessDeps,
): Promise<Response> {
  const { appOrigin } = deps;
  if (!appOrigin) return text(503, "Service unavailable.");
  if (request.headers.get("origin") !== appOrigin) {
    deps.log("process document refused: origin_mismatch");
    return text(403, "Forbidden.");
  }
  if (![tenantId, workspaceId, documentId].every(isCanonicalUuid)) {
    return text(404, "Not found.");
  }

  const pagePath = `/tenants/${tenantId}/workspaces/${workspaceId}/documents/${documentId}`;
  const back = (code?: string) =>
    redirect(`${appOrigin}${pagePath}${code ? `?processing=${code}` : ""}`);
  const login = () =>
    redirect(
      `${appOrigin}/auth/login?returnTo=${encodeURIComponent(pagePath)}`,
    );

  const session = await deps.readSession();
  if (!session) return login();

  const result = await deps.start(
    session.accessToken,
    tenantId,
    workspaceId,
    documentId,
  );
  switch (result.state) {
    case "started":
      // An existing run is shown as it is; only a new one is announced.
      return back(result.created ? "started" : undefined);
    case "not_scanned":
    case "blocked":
    case "processing_unavailable":
      return back(result.state);
    case "not_found":
      // The document page renders the uniform "not found or no access" page.
      return back();
    case "unauthorized":
      if (reauthenticateOnUnauthorized(session, deps.now())) return login();
      return back("rejected");
    case "unavailable":
      deps.log("process document failed: api_unavailable");
      return back("unavailable");
  }
}

function redirect(location: string): Response {
  return new Response(null, {
    status: 303,
    headers: { Location: location, ...NO_STORE },
  });
}

function text(status: number, body: string): Response {
  return new Response(body, {
    status,
    headers: { "Content-Type": "text/plain; charset=utf-8", ...NO_STORE },
  });
}
