// The only module that talks to the API's liveness endpoint. It runs on the
// Next.js server; the browser never calls the API directly.

export type ApiLiveness =
  | { state: "up" }
  | { state: "not-configured" }
  | { state: "unreachable"; reason: string };

const TIMEOUT_MS = 2000;

export async function getApiLiveness(
  baseUrl: string | undefined,
  fetchImpl: typeof fetch = fetch,
): Promise<ApiLiveness> {
  if (!baseUrl) {
    return { state: "not-configured" };
  }

  let response: Response;
  try {
    response = await fetchImpl(new URL("/health/live", baseUrl), {
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (error) {
    return { state: "unreachable", reason: describeError(error) };
  }

  if (!response.ok) {
    return { state: "unreachable", reason: `HTTP ${response.status}` };
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return { state: "unreachable", reason: "response was not JSON" };
  }

  if (!isLivenessBody(body)) {
    return { state: "unreachable", reason: "unexpected response body" };
  }
  return { state: "up" };
}

function isLivenessBody(body: unknown): body is { status: "ok" } {
  return (
    typeof body === "object" &&
    body !== null &&
    "status" in body &&
    body.status === "ok"
  );
}

function describeError(error: unknown): string {
  if (error instanceof DOMException && error.name === "TimeoutError") {
    return `no response within ${TIMEOUT_MS} ms`;
  }
  return error instanceof Error ? error.message : "request failed";
}
