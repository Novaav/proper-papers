import type { NextRequest } from "next/server.js";

import { createWorkspace } from "@/lib/api/workspaces.ts";
import { loadAuthConfig } from "@/lib/auth/config.ts";
import { nowSeconds, readSession } from "@/lib/auth/server.ts";
import { handleCreateWorkspace } from "@/lib/workspaces/create-handler.ts";

export const dynamic = "force-dynamic";

// POST only: any other method gets 405 from Next.js.
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ tenantId: string }> },
) {
  const { tenantId } = await params;
  const config = loadAuthConfig(process.env);
  return handleCreateWorkspace(request, tenantId, {
    appOrigin: config.ok ? config.config.appOrigin : null,
    readSession,
    createWorkspace: (token, tenant, name) =>
      createWorkspace(process.env.API_BASE_URL, token, tenant, name),
    now: nowSeconds,
    log: console.warn,
  });
}
