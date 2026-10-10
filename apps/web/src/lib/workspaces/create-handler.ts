// POST /tenants/{tenantId}/workspaces/create: the HTML form's target.
//
// Order of checks: configuration, exact Origin (CSRF), tenant ID format,
// session, body, name. Membership is not checked here: the API decides, on
// every call, and a non-member gets the same "not found" as a missing tenant.
// The response is always a redirect (post/redirect/get) to the list page with
// a fixed status code, or a plain-text error; request input is never echoed.

import { isCanonicalUuid } from "../ids.ts";
import type { CreateResult } from "../api/workspaces.ts";
import { reauthenticateOnUnauthorized } from "../auth/redirect.ts";
import type { Session } from "../auth/session.ts";

export const NAME_MAX_LENGTH = 200;
export const MAX_FORM_BYTES = 8192;

export interface CreateDeps {
  /** APP_ORIGIN, or null when authentication is not configured. */
  appOrigin: string | null;
  readSession: () => Promise<Session | null>;
  createWorkspace: (
    accessToken: string,
    tenantId: string,
    name: string,
  ) => Promise<CreateResult>;
  /** Epoch seconds. */
  now: () => number;
  /** Receives a fixed category, never names, IDs of other tenants or tokens. */
  log: (message: string) => void;
}

const NO_STORE = { "Cache-Control": "no-store" };

export async function handleCreateWorkspace(
  request: Request,
  tenantId: string,
  deps: CreateDeps,
): Promise<Response> {
  const { appOrigin } = deps;
  if (!appOrigin) return text(503, "Service unavailable.");
  if (request.headers.get("origin") !== appOrigin) {
    deps.log("create workspace refused: origin_mismatch");
    return text(403, "Forbidden.");
  }
  if (!isCanonicalUuid(tenantId)) return text(404, "Not found.");

  const listPath = `/tenants/${tenantId}/workspaces`;
  const back = (query: string) => redirect(`${appOrigin}${listPath}?${query}`);
  const login = () =>
    redirect(
      `${appOrigin}/auth/login?returnTo=${encodeURIComponent(listPath)}`,
    );

  const session = await deps.readSession();
  if (!session) return login();

  const form = await readForm(request);
  if (form === "bad_request") return text(400, "Bad request.");
  const names = form === "too_large" ? [] : form.getAll("name");
  const name = names.length === 1 ? names[0] : undefined;
  if (typeof name !== "string" || !isValidName(name)) {
    return back("error=invalid_name");
  }

  const result = await deps.createWorkspace(
    session.accessToken,
    tenantId,
    name,
  );
  switch (result.state) {
    case "created":
      return back("created=1");
    case "invalid_name":
      return back("error=invalid_name");
    case "not_found":
      // The list page renders the uniform "not found or no access" page.
      return redirect(`${appOrigin}${listPath}`);
    case "unauthorized":
      if (reauthenticateOnUnauthorized(session, deps.now())) return login();
      return back("error=rejected");
    case "unavailable":
      deps.log("create workspace failed: api_unavailable");
      return back("error=unavailable");
  }
}

/** Mirrors the API rule: 1..200 characters after trimming spaces, no NUL. */
export function isValidName(name: string): boolean {
  if (name.includes("\u0000")) return false;
  const length = [...name.replace(/^ +| +$/g, "")].length;
  return length >= 1 && length <= NAME_MAX_LENGTH;
}

async function readForm(
  request: Request,
): Promise<URLSearchParams | "too_large" | "bad_request"> {
  const type = request.headers
    .get("content-type")
    ?.split(";")[0]
    ?.trim()
    .toLowerCase();
  if (type !== "application/x-www-form-urlencoded" || !request.body) {
    return "bad_request";
  }
  const declared = request.headers.get("content-length");
  if (declared && /^\d+$/.test(declared) && Number(declared) > MAX_FORM_BYTES) {
    return "too_large";
  }
  const chunks: Uint8Array[] = [];
  let size = 0;
  const reader = request.body.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_FORM_BYTES) {
      await reader.cancel();
      return "too_large";
    }
    chunks.push(value);
  }
  try {
    const decoded = new TextDecoder("utf-8", { fatal: true }).decode(
      Buffer.concat(chunks),
    );
    return new URLSearchParams(decoded);
  } catch {
    return "bad_request";
  }
}

function redirect(location: string): Response {
  return new Response(null, {
    status: 303,
    headers: { ...NO_STORE, Location: location },
  });
}

function text(status: number, body: string): Response {
  return new Response(body, {
    status,
    headers: { ...NO_STORE, "Content-Type": "text/plain; charset=utf-8" },
  });
}
