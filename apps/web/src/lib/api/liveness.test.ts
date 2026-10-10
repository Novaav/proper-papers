import assert from "node:assert/strict";
import { test } from "node:test";

import { getApiLiveness } from "./liveness.ts";

const BASE_URL = "http://api.test";

function respondWith(response: Response): typeof fetch {
  return () => Promise.resolve(response);
}

test("reports not-configured without calling the API when no base URL is set", async () => {
  const neverCalled: typeof fetch = () =>
    assert.fail("fetch should not be called");

  assert.deepEqual(await getApiLiveness(undefined, neverCalled), {
    state: "not-configured",
  });
  assert.deepEqual(await getApiLiveness("", neverCalled), {
    state: "not-configured",
  });
});

test("requests /health/live on the configured base URL", async () => {
  let requested = "";
  const fetchImpl: typeof fetch = (input) => {
    requested = input instanceof URL ? input.href : String(input);
    return Promise.resolve(Response.json({ status: "ok" }));
  };

  await getApiLiveness(BASE_URL, fetchImpl);

  assert.equal(requested, "http://api.test/health/live");
});

test("reports up only for a 200 response with the documented body", async () => {
  const result = await getApiLiveness(
    BASE_URL,
    respondWith(Response.json({ status: "ok" })),
  );

  assert.deepEqual(result, { state: "up" });
});

test("reports unreachable for a non-2xx response", async () => {
  const result = await getApiLiveness(
    BASE_URL,
    respondWith(new Response("unavailable", { status: 503 })),
  );

  assert.deepEqual(result, { state: "unreachable", reason: "HTTP 503" });
});

test("reports unreachable when a 200 response has an unexpected body", async () => {
  const result = await getApiLiveness(
    BASE_URL,
    respondWith(Response.json({ status: "degraded" })),
  );

  assert.deepEqual(result, {
    state: "unreachable",
    reason: "unexpected response body",
  });
});

test("reports unreachable when the body is not JSON", async () => {
  const result = await getApiLiveness(
    BASE_URL,
    respondWith(new Response("<html>")),
  );

  assert.deepEqual(result, {
    state: "unreachable",
    reason: "response was not JSON",
  });
});

test("reports unreachable when the request fails", async () => {
  const failing: typeof fetch = () =>
    Promise.reject(new TypeError("fetch failed"));

  assert.deepEqual(await getApiLiveness(BASE_URL, failing), {
    state: "unreachable",
    reason: "fetch failed",
  });
});
