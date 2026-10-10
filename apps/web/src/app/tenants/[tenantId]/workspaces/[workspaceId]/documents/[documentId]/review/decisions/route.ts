import type { NextRequest } from "next/server.js";

import { postDecision } from "@/lib/api/review.ts";
import { loadAuthConfig } from "@/lib/auth/config.ts";
import { nowSeconds, readSession } from "@/lib/auth/server.ts";
import { handleDecision } from "@/lib/review/handlers.ts";

export const dynamic = "force-dynamic";

// POST only (a review decision form); any other method gets 405.
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
  return handleDecision(request, tenantId, workspaceId, documentId, {
    appOrigin: config.ok ? config.config.appOrigin : null,
    readSession,
    decide: (token, tenant, workspace, document, input) =>
      postDecision(
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
