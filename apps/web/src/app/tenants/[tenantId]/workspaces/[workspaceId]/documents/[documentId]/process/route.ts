import type { NextRequest } from "next/server.js";

import { startProcessing } from "@/lib/api/processing.ts";
import { loadAuthConfig } from "@/lib/auth/config.ts";
import { nowSeconds, readSession } from "@/lib/auth/server.ts";
import { handleProcess } from "@/lib/processing/process-handler.ts";

export const dynamic = "force-dynamic";

// POST only (the "Process document" form); any other method gets 405.
export async function POST(
  request: NextRequest,
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
  const config = loadAuthConfig(process.env);
  return handleProcess(request, tenantId, workspaceId, documentId, {
    appOrigin: config.ok ? config.config.appOrigin : null,
    readSession,
    start: (token, tenant, workspace, document) =>
      startProcessing(
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
