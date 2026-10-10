import type { NextRequest } from "next/server.js";

import { getWorkspace, uploadDocument } from "@/lib/api/documents.ts";
import { loadAuthConfig } from "@/lib/auth/config.ts";
import { nowSeconds, readSession } from "@/lib/auth/server.ts";
import { handleUpload } from "@/lib/documents/upload-handler.ts";

export const dynamic = "force-dynamic";

// POST only (other methods get 405). Excluded from src/proxy.ts: the proxy's
// request-body buffering would truncate uploads over 10 MB.
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ tenantId: string; workspaceId: string }> },
) {
  const { tenantId, workspaceId } = await params;
  const config = loadAuthConfig(process.env);
  return handleUpload(request, tenantId, workspaceId, {
    appOrigin: config.ok ? config.config.appOrigin : null,
    readSession,
    upload: (token, tenant, workspace, file) =>
      uploadDocument(process.env.API_BASE_URL, token, tenant, workspace, file),
    workspace: (token, tenant, workspace) =>
      getWorkspace(process.env.API_BASE_URL, token, tenant, workspace),
    now: nowSeconds,
    log: console.warn,
  });
}
