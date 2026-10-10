// GET /v1/me with the session's access token. Server-side only; the token is
// sent to the API and nowhere else.

export interface Me {
  userId: string;
  memberships: { tenantId: string; role: "owner" | "reviewer" | "member" }[];
}

export type MeResult =
  | { state: "ok"; me: Me }
  | { state: "unauthorized" }
  | { state: "unavailable"; reason: string };

const TIMEOUT_MS = 5000;

export async function getMe(
  baseUrl: string | undefined,
  accessToken: string,
  fetchImpl: typeof fetch = fetch,
): Promise<MeResult> {
  if (!baseUrl)
    return { state: "unavailable", reason: "API_BASE_URL is not set" };
  let response: Response;
  try {
    response = await fetchImpl(new URL("/v1/me", baseUrl), {
      headers: { Authorization: `Bearer ${accessToken}` },
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch {
    return { state: "unavailable", reason: "API unreachable" };
  }
  if (response.status === 401) return { state: "unauthorized" };
  if (!response.ok)
    return { state: "unavailable", reason: `HTTP ${response.status}` };
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return { state: "unavailable", reason: "response was not JSON" };
  }
  const me = parseMe(body);
  return me
    ? { state: "ok", me }
    : { state: "unavailable", reason: "unexpected response body" };
}

function parseMe(body: unknown): Me | null {
  if (typeof body !== "object" || body === null) return null;
  const { user_id: userId, memberships } = body as Record<string, unknown>;
  if (typeof userId !== "string" || !Array.isArray(memberships)) return null;
  const parsed: Me["memberships"] = [];
  for (const item of memberships) {
    if (typeof item !== "object" || item === null) return null;
    const { tenant_id: tenantId, role } = item as Record<string, unknown>;
    if (
      typeof tenantId !== "string" ||
      (role !== "owner" && role !== "reviewer" && role !== "member")
    )
      return null;
    parsed.push({ tenantId, role });
  }
  return { userId, memberships: parsed };
}
