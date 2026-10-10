import { downloadDocument } from "@/lib/api/documents.ts";
import { nowSeconds, readSession } from "@/lib/auth/server.ts";
import { handleDownload } from "@/lib/documents/download-handler.ts";

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
    }>;
  },
) {
  const { tenantId, workspaceId, documentId } = await params;
  return handleDownload(tenantId, workspaceId, documentId, {
    readSession,
    download: (token, tenant, workspace, document) =>
      downloadDocument(
        process.env.API_BASE_URL,
        token,
        tenant,
        workspace,
        document,
      ),
    now: nowSeconds,
    log: console.warn,
  });
}
