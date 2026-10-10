import Link from "next/link.js";
import type { ReactNode } from "react";

import { BrandMark } from "./brand-mark.tsx";
import { RailNav, type RailWorkspace } from "./rail-nav.tsx";
import { ThemeToggle } from "./theme-toggle.tsx";

// The signed-in frame: a persistent rail on the left (Workspaces, Account and,
// inside a tenant, a switcher for that tenant's workspaces), and the page on a
// framed work surface. It only renders what its layout passes in: it reads no
// session itself, and every link is a route that exists.
export function AppShell({
  workspacesHref,
  workspaces,
  children,
}: {
  // Only known when the tenant is known; null leaves the link out.
  workspacesHref: string | null;
  // null when the list is unavailable; the rail then shows only its fixed links.
  workspaces: RailWorkspace[] | null;
  children: ReactNode;
}) {
  return (
    <div className="app-frame">
      <aside className="rail">
        <Link className="rail-brand" href="/">
          <BrandMark />
          <span>Proper Papers</span>
        </Link>
        <RailNav workspacesHref={workspacesHref} workspaces={workspaces} />
        <div className="rail-foot">
          <ThemeToggle variant="rail" />
          <p className="rail-note">
            Synthetic documents only. A portfolio project; nothing is deployed.
          </p>
        </div>
      </aside>
      <div className="app-content">{children}</div>
    </div>
  );
}
