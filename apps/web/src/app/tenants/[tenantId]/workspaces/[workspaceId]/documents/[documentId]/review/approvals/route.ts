import type { NextRequest } from "next/server.js";

import { postApproval } from "@/lib/api/review.ts";
import { loadAuthConfig } from "@/lib/auth/config.ts";
import { nowSeconds, readSession } from "@/lib/auth/server.ts";
import { handleApproval } from "@/lib/review/handlers.ts";

export const dynamic = "force-dynamic";

// POST only (the approve form); any other method gets 405.
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
  return handleApproval(request, tenantId, workspaceId, documentId, {
    appOrigin: config.ok ? config.config.appOrigin : null,
    readSession,
    approve: (token, tenant, workspace, document, input) =>
      postApproval(
        process.env.API_BASE_URL,
        token,
        tenant,
        workspace,
        document,
        input,
      ),
    now: nowSeconds,
    log: console.warn,
  });
}
