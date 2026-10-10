"use client";

import { usePathname } from "next/navigation.js";

// The rail's links. It is a client component only to know the current path, so
// the place you are in is marked (aria-current); the links themselves are plain
// anchors and work without JavaScript. Every href is a route that exists.
export interface RailWorkspace {
  id: string;
  name: string;
  href: string;
}

const MAX_LISTED = 12;

export function RailNav({
  workspacesHref,
  workspaces,
}: {
  workspacesHref: string | null;
  workspaces: RailWorkspace[] | null;
}) {
  const path = usePathname();
  const here = (href: string) => (path === href ? "page" : undefined);
  const inside = (href: string) =>
    path === href ? "page" : path.startsWith(`${href}/`) ? "true" : undefined;
  const listed = workspaces?.slice(0, MAX_LISTED) ?? [];
  return (
    <nav aria-label="Primary" className="rail-nav">
      <ul>
        {workspacesHref && (
          <li>
            <a href={workspacesHref} aria-current={here(workspacesHref)}>
              <WorkspacesIcon />
              Workspaces
            </a>
          </li>
        )}
        <li>
          <a href="/account" aria-current={here("/account")}>
            <AccountIcon />
            Account
          </a>
        </li>
      </ul>
      {listed.length > 0 && (
        <>
          <p className="rail-label" id="rail-workspaces">
            Your workspaces
            <span className="rail-count">{workspaces?.length ?? 0}</span>
          </p>
          <ul aria-labelledby="rail-workspaces" className="rail-workspaces">
            {listed.map((workspace) => (
              <li key={workspace.id}>
                <a
                  href={workspace.href}
                  title={workspace.name}
                  aria-current={inside(workspace.href)}
                >
                  <span className="rail-dot" aria-hidden="true" />
                  <span className="rail-name">{workspace.name}</span>
                </a>
              </li>
            ))}
          </ul>
        </>
      )}
    </nav>
  );
}

function WorkspacesIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <rect x="2" y="2" width="5" height="5" rx="1.2" />
      <rect x="9" y="2" width="5" height="5" rx="1.2" />
      <rect x="2" y="9" width="5" height="5" rx="1.2" />
      <rect x="9" y="9" width="5" height="5" rx="1.2" />
    </svg>
  );
}

function AccountIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <circle cx="8" cy="5.5" r="2.5" />
      <path d="M3 13.5c.6-2.3 2.5-3.5 5-3.5s4.4 1.2 5 3.5" />
    </svg>
  );
}
