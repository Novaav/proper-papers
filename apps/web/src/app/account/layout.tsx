import type { ReactNode } from "react";

import { getMeOnce, listWorkspacesOnce } from "@/lib/api/once.ts";
import { readSession } from "@/lib/auth/server.ts";
import { isCanonicalUuid } from "@/lib/ids.ts";
import { newestFirst } from "@/lib/workspaces/order.ts";

import { AppShell } from "../app-shell.tsx";

// The account page has no tenant of its own, so the rail's Workspaces link
// goes to the user's first tenant (the page lists every tenant), and the
// workspace shortcuts appear when the user has exactly one tenant. The session is
// only read here; the page does the sign-in redirects.
export default async function AccountLayout({
  children,
}: {
  children: ReactNode;
}) {
  const session = await readSession();
  let workspacesHref: string | null = null;
  let workspaces: { id: string; name: string; href: string }[] | null = null;
  if (session) {
    const base = process.env.API_BASE_URL;
    const me = await getMeOnce(base, session.accessToken);
    const memberships = me.state === "ok" ? me.me.memberships : [];
    const first = memberships[0];
    if (first && isCanonicalUuid(first.tenantId)) {
      workspacesHref = `/tenants/${first.tenantId}/workspaces`;
      // Shortcuts only when the tenant is unambiguous: with several tenants the
      // account page itself lists them and the rail does not guess.
      if (memberships.length === 1) {
        const list = await listWorkspacesOnce(
          base,
          session.accessToken,
          first.tenantId,
        );
        if (list.state === "ok") {
          workspaces = newestFirst(list.workspaces).map((workspace) => ({
            id: workspace.id,
            name: workspace.name,
            href: `${workspacesHref}/${workspace.id}`,
          }));
        }
      }
    }
  }
  return (
    <AppShell workspacesHref={workspacesHref} workspaces={workspaces}>
      {children}
    </AppShell>
  );
}
