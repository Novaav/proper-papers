import { getProcessing } from "@/lib/api/processing.ts";
import { readSession } from "@/lib/auth/server.ts";
import { handleStatus } from "@/lib/processing/status-handler.ts";

export const dynamic = "force-dynamic";

// GET only: the document page's live status check (JSON, state only).
export async function GET(
  _request: Request,
  {
    params,
  }: {
    params: Promise<{
      tenantId: string;
      workspaceId: string;
      documentId: string;
    }>;
  },
) {
  const { tenantId, workspaceId, documentId } = await params;
  return handleStatus(tenantId, workspaceId, documentId, {
    readSession,
    getProcessing: (token, tenant, workspace, document) =>
      getProcessing(
        process.env.API_BASE_URL,
        token,
        tenant,
        workspace,
        document,
      ),
    log: console.warn,
  });
}
