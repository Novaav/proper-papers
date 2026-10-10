// GET /tenants/{t}/workspaces/{w}/documents/{d}/processing: the document page's
// live status check, as JSON (same origin; the session's token stays on the
// server). It answers only the run's state, never the result or any document
// text: the page renders results on the server when the run has finished.

import type { ProcessingResult } from "../api/processing.ts";
import type { Session } from "../auth/session.ts";
import { isCanonicalUuid } from "../ids.ts";

export interface StatusDeps {
  readSession: () => Promise<Session | null>;
  getProcessing: (
    accessToken: string,
    tenantId: string,
    workspaceId: string,
    documentId: string,
  ) => Promise<ProcessingResult>;
  log: (message: string) => void;
}

export async function handleStatus(
  tenantId: string,
  workspaceId: string,
  documentId: string,
  deps: StatusDeps,
): Promise<Response> {
  if (![tenantId, workspaceId, documentId].every(isCanonicalUuid)) {
    return answer(404, { state: "not_found" });
  }
  const session = await deps.readSession();
  if (!session) return answer(401, { state: "signed_out" });
  const result = await deps.getProcessing(
    session.accessToken,
    tenantId,
    workspaceId,
    documentId,
  );
  switch (result.state) {
    case "ok":
      return result.run === null
        ? answer(200, { state: "none" })
        : answer(200, {
            state: "ok",
            status: result.run.status,
            attempts: result.run.attempts,
            startedAt: result.run.startedAt,
          });
    case "not_scanned":
    case "blocked":
      return answer(409, { state: "not_clean" });
    case "not_found":
      return answer(404, { state: "not_found" });
    case "unauthorized":
      // The page decides whether to sign in again; this answer never redirects.
      return answer(401, { state: "signed_out" });
    case "unavailable":
      deps.log("processing status failed: api_unavailable");
      return answer(503, { state: "unavailable" });
  }
}

function answer(status: number, body: object): Response {
  return Response.json(body, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}
