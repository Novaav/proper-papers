import { notFound, redirect } from "next/navigation.js";

import { listWorkspacesOnce } from "@/lib/api/once.ts";
import { type Workspace } from "@/lib/api/workspaces.ts";
import { reauthenticateOnUnauthorized } from "@/lib/auth/redirect.ts";
import { loginPath, nowSeconds, readSession } from "@/lib/auth/server.ts";
import { isCanonicalUuid } from "@/lib/ids.ts";
import { NAME_MAX_LENGTH } from "@/lib/workspaces/create-handler.ts";
import { newestFirst } from "@/lib/workspaces/order.ts";

import { Toolbar } from "../../../toolbar.tsx";
import { NewWorkspace } from "./new-workspace.tsx";

export const dynamic = "force-dynamic";

// Fixed messages for the codes the create route redirects with; any other
// value is ignored, so nothing from the URL is ever rendered.
const SUCCESS: Record<string, string> = { "1": "Workspace created." };
const ERRORS: Record<string, string> = {
  invalid_name:
    "Enter a name of 1 to 200 characters (leading and trailing spaces do not count).",
  unavailable: "The workspace could not be created right now. Try again.",
  rejected:
    "The API rejected this sign-in. Sign out and try again from your account page.",
};

interface Props {
  params: Promise<{ tenantId: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

// Server Component: reads the session, never changes cookies. The API decides
// membership on every call; this page only renders its answer.
export default async function WorkspacesPage({ params, searchParams }: Props) {
  const { tenantId } = await params;
  if (!isCanonicalUuid(tenantId)) notFound();
  const path = `/tenants/${tenantId}/workspaces`;

  const session = await readSession();
  if (!session) redirect(loginPath(path));

  const result = await listWorkspacesOnce(
    process.env.API_BASE_URL,
    session.accessToken,
    tenantId,
  );
  if (result.state === "not_found") notFound();
  if (
    result.state === "unauthorized" &&
    reauthenticateOnUnauthorized(session, nowSeconds())
  ) {
    redirect(loginPath(path));
  }

  const query = await searchParams;
  const success = lookup(SUCCESS, query.created);
  const error =
    result.state === "unauthorized"
      ? ERRORS.rejected
      : lookup(ERRORS, query.error);
  const nameError = query.error === "invalid_name";

  return (
    <main id="main" tabIndex={-1} className="page">
      <Toolbar
        crumbs={[
          { label: "Account", href: "/account" },
          { label: "Workspaces" },
        ]}
      >
        {result.state !== "unauthorized" && (
          <NewWorkspace
            action={`${path}/create`}
            maxLength={NAME_MAX_LENGTH}
            open={
              nameError ||
              (result.state === "ok" && result.workspaces.length === 0)
            }
            nameError={nameError}
            errorText={nameError ? (ERRORS.invalid_name ?? "") : ""}
          />
        )}
      </Toolbar>
      <div className="page-body">
        <header className="page-head">
          <h1>Workspaces</h1>
          <p className="meta">
            Documents live in workspaces. Open one to upload and review them.
          </p>
        </header>

        {success && <p role="status">{success}</p>}
        {error && !nameError && <p role="alert">{error}</p>}

        <section className="panel" aria-labelledby="workspace-list">
          <h2 id="workspace-list">All workspaces</h2>
          {result.state === "ok" && result.workspaces.length > 0 && (
            <p className="panel-count">
              {result.workspaces.length === 1
                ? "1 workspace"
                : `${result.workspaces.length} workspaces`}
              , newest first
            </p>
          )}
          {result.state === "ok" ? (
            <WorkspaceList path={path} workspaces={result.workspaces} />
          ) : result.state === "unavailable" ? (
            <p role="alert">
              Workspaces are unavailable right now. <a href={path}>Try again</a>
              .
            </p>
          ) : null}
        </section>
        <details className="id-details">
          <summary>Details</summary>
          <dl>
            <dt>Tenant ID</dt>
            <dd>
              <code>{tenantId}</code>
            </dd>
          </dl>
        </details>
      </div>
    </main>
  );
}

function WorkspaceList({
  path,
  workspaces,
}: {
  path: string;
  workspaces: Workspace[];
}) {
  if (workspaces.length === 0) {
    return (
      <>
        <p>No workspaces yet.</p>
        <p className="hint">
          Use New workspace to create the first one; documents are uploaded
          inside a workspace.
        </p>
      </>
    );
  }
  return (
    <ul className="workspaces">
      {newestFirst(workspaces).map((workspace) => (
        <li key={workspace.id}>
          <a className="name" href={`${path}/${workspace.id}`}>
            {workspace.name}
          </a>{" "}
          <span className="meta">
            created{" "}
            <time dateTime={workspace.createdAt}>
              {format(workspace.createdAt)}
            </time>
          </span>
        </li>
      ))}
    </ul>
  );
}

function lookup(
  messages: Record<string, string>,
  value: string | string[] | undefined,
): string | undefined {
  return typeof value === "string" && Object.hasOwn(messages, value)
    ? messages[value]
    : undefined;
}

function format(iso: string): string {
  return `${new Date(iso).toLocaleString("en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  })} UTC`;
}
