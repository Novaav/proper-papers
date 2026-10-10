import { exportApproval } from "@/lib/api/review.ts";
import { nowSeconds, readSession } from "@/lib/auth/server.ts";
import { handleExport } from "@/lib/review/handlers.ts";

export const dynamic = "force-dynamic";

// Excluded from src/proxy.ts: its page policy would replace the sandbox
// Content-Security-Policy this route sets on the file.

export async function GET(
  _request: Request,
  {
    params,
  }: {
    params: Promise<{
      tenantId: string;
      workspaceId: string;
      documentId: string;
      version: string;
    }>;
  },
) {
  const { tenantId, workspaceId, documentId, version } = await params;
  return handleExport(tenantId, workspaceId, documentId, version, {
    readSession,
    export: (token, tenant, workspace, document, number) =>
      exportApproval(
        process.env.API_BASE_URL,
        token,
        tenant,
        workspace,
        document,
        number,
      ),
    now: nowSeconds,
    log: console.warn,
  });
}
