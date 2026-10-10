import { redirect } from "next/navigation.js";
import { Fragment } from "react";

import { getMeOnce } from "@/lib/api/once.ts";
import { reauthenticateOnUnauthorized } from "@/lib/auth/redirect.ts";
import { loginPath, nowSeconds, readSession } from "@/lib/auth/server.ts";
import { isCanonicalUuid } from "@/lib/ids.ts";

import { Toolbar } from "../toolbar.tsx";

export const dynamic = "force-dynamic";

// Server Component: it reads the session but never changes cookies. Any
// invalid session (missing, expired, or rejected by the API) redirects to the
// login route handler, whose response clears the session cookie.
export default async function AccountPage() {
  const session = await readSession();
  if (!session) redirect(loginPath("/account"));

  const result = await getMeOnce(process.env.API_BASE_URL, session.accessToken);
  const rejected = result.state === "unauthorized";
  if (rejected && reauthenticateOnUnauthorized(session, nowSeconds())) {
    redirect(loginPath("/account"));
  }

  return (
    <main id="main" tabIndex={-1} className="page">
      <Toolbar crumbs={[{ label: "Account" }]} />
      <div className="page-body page-body--narrow">
        <h1>Account</h1>
        {rejected ? (
          <p>
            The API rejected this sign-in. Sign out and try again; if it keeps
            happening, the web and API authentication settings do not match.
          </p>
        ) : result.state === "ok" ? (
          <>
            <section className="panel" aria-labelledby="memberships-title">
              <h2 id="memberships-title">Tenant memberships</h2>
              {result.me.memberships.length === 0 ? (
                <p>You are not a member of any tenant yet.</p>
              ) : (
                <ul className="workspaces memberships">
                  {result.me.memberships.map((m) => (
                    <li key={m.tenantId}>
                      {isCanonicalUuid(m.tenantId) ? (
                        <a
                          className="name"
                          href={`/tenants/${m.tenantId}/workspaces`}
                        >
                          Workspaces
                          <span className="visually-hidden">
                            {" "}
                            of tenant {m.tenantId}, your role: {m.role}
                          </span>
                        </a>
                      ) : (
                        <span className="name">
                          <code>{m.tenantId}</code>
                        </span>
                      )}
                      <span className="meta">
                        <span className="badge">{m.role}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
            <details className="id-details">
              <summary>Technical details</summary>
              <dl>
                <dt>User ID</dt>
                <dd>
                  <code>{result.me.userId}</code>
                </dd>
                {result.me.memberships.map((m) => (
                  <Fragment key={m.tenantId}>
                    <dt>Tenant ID</dt>
                    <dd>
                      <code>{m.tenantId}</code>
                    </dd>
                  </Fragment>
                ))}
              </dl>
            </details>
          </>
        ) : result.state === "unavailable" ? (
          <p>
            Your account details are unavailable right now ({result.reason}).
          </p>
        ) : null}
        <form method="post" action="/auth/logout">
          <button type="submit">Sign out</button>
        </form>
      </div>
    </main>
  );
}
