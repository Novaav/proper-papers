import type { ReactNode } from "react";

import { listWorkspacesOnce } from "@/lib/api/once.ts";
import { readSession } from "@/lib/auth/server.ts";
import { isCanonicalUuid } from "@/lib/ids.ts";
import { newestFirst } from "@/lib/workspaces/order.ts";

import { AppShell } from "../../app-shell.tsx";

// The same frame for everything under a tenant. The tenant ID only builds the
// rail's links, and only when it is a well-formed ID; the workspace switcher is
// the tenant's own list from the API (which authorizes the call, so a foreign
// or unknown tenant gets no list). The session is only read here; the pages do
// the sign-in redirects and the not-found answers.
export default async function TenantLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ tenantId: string }>;
}) {
  const { tenantId } = await params;
  const known = isCanonicalUuid(tenantId);
  const base = `/tenants/${tenantId}/workspaces`;
  const session = known ? await readSession() : null;
  const result = session
    ? await listWorkspacesOnce(
        process.env.API_BASE_URL,
        session.accessToken,
        tenantId,
      )
    : null;
  return (
    <AppShell
      workspacesHref={known ? base : null}
      workspaces={
        result?.state === "ok"
          ? newestFirst(result.workspaces).map((workspace) => ({
              id: workspace.id,
              name: workspace.name,
              href: `${base}/${workspace.id}`,
            }))
          : null
      }
    >
      {children}
    </AppShell>
  );
}
