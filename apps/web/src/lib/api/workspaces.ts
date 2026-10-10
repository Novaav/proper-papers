// Tenant workspace endpoints of the API, called server-side with the
// session's access token. The API authorizes every call (membership); this
// module only relays and validates. Tenant IDs are checked before they are
// placed in a URL path.

import { isCanonicalUuid } from "../ids.ts";

export interface Workspace {
  id: string;
  name: string;
  createdAt: string;
}

export type ListResult =
  | { state: "ok"; workspaces: Workspace[] }
  | { state: "not_found" }
  | { state: "unauthorized" }
  | { state: "unavailable" };

export type CreateResult =
  | { state: "created"; workspace: Workspace }
  | { state: "invalid_name" }
  | { state: "not_found" }
  | { state: "unauthorized" }
  | { state: "unavailable" };

const TIMEOUT_MS = 5000;

export async function listWorkspaces(
  baseUrl: string | undefined,
  accessToken: string,
  tenantId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<ListResult> {
  if (!isCanonicalUuid(tenantId)) return { state: "not_found" };
  const response = await call(baseUrl, accessToken, tenantId, fetchImpl);
  if (response === null) return { state: "unavailable" };
  if (response.status === 404) return { state: "not_found" };
  if (response.status === 401) return { state: "unauthorized" };
  if (response.status !== 200) return { state: "unavailable" };
  const body = await json(response);
  const items = (body as { workspaces?: unknown } | null)?.workspaces;
  if (!Array.isArray(items)) return { state: "unavailable" };
  const workspaces: Workspace[] = [];
  for (const item of items) {
    const workspace = parseWorkspace(item);
    if (!workspace) return { state: "unavailable" };
    workspaces.push(workspace);
  }
  return { state: "ok", workspaces };
}

export async function createWorkspace(
  baseUrl: string | undefined,
  accessToken: string,
  tenantId: string,
  name: string,
  fetchImpl: typeof fetch = fetch,
): Promise<CreateResult> {
  if (!isCanonicalUuid(tenantId)) return { state: "not_found" };
  const response = await call(baseUrl, accessToken, tenantId, fetchImpl, {
    name,
  });
  if (response === null) return { state: "unavailable" };
  if (response.status === 422) return { state: "invalid_name" };
  if (response.status === 404) return { state: "not_found" };
  if (response.status === 401) return { state: "unauthorized" };
  if (response.status !== 201) return { state: "unavailable" };
  const workspace = parseWorkspace(await json(response));
  return workspace ? { state: "created", workspace } : { state: "unavailable" };
}

async function call(
  baseUrl: string | undefined,
  accessToken: string,
  tenantId: string,
  fetchImpl: typeof fetch,
  body?: { name: string },
): Promise<Response | null> {
  if (!baseUrl) return null;
  try {
    return await fetchImpl(
      new URL(`/v1/tenants/${tenantId}/workspaces`, baseUrl),
      {
        method: body ? "POST" : "GET",
        headers: {
          Authorization: `Bearer ${accessToken}`,
          ...(body ? { "Content-Type": "application/json" } : {}),
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
        cache: "no-store",
        redirect: "error",
        signal: AbortSignal.timeout(TIMEOUT_MS),
      },
    );
  } catch {
    return null;
  }
}

async function json(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

export function parseWorkspace(value: unknown): Workspace | null {
  if (typeof value !== "object" || value === null) return null;
  const { id, name, created_at: createdAt } = value as Record<string, unknown>;
  if (
    !isCanonicalUuid(id) ||
    typeof name !== "string" ||
    typeof createdAt !== "string" ||
    Number.isNaN(Date.parse(createdAt))
  ) {
    return null;
  }
  return { id, name, createdAt };
}
