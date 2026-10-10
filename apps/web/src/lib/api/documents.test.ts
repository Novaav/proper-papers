import assert from "node:assert/strict";
import { test } from "node:test";

import {
  downloadDocument,
  getDocument,
  getWorkspace,
  listDocuments,
  uploadDocument,
} from "./documents.ts";

const BASE = "http://api.test";
const TENANT = "01a0e9bd-65a9-700c-a38b-c2210bcb6380";
const WORKSPACE = "01a0e9d1-6f65-7ac6-b4b3-84557cd2b17f";
const DOCUMENT = "01a0f000-0000-7000-8000-000000000001";
const SHA = "a".repeat(64);
const API_DOCUMENT = {
  id: DOCUMENT,
  workspace_id: WORKSPACE,
  name: "Report.pdf",
  created_at: "2026-09-29T10:00:00.000000+00:00",
  version: {
    id: "01a0f000-0000-7000-8000-000000000002",
    number: 1,
    media_type: "application/pdf",
    size_bytes: 1234,
    sha256: SHA,
    scan_status: "clean",
  },
};
const SUMMARY = {
  id: DOCUMENT,
  name: "Report.pdf",
  createdAt: API_DOCUMENT.created_at,
  sizeBytes: 1234,
  sha256: SHA,
  scanStatus: "clean",
};
const PDF = new TextEncoder().encode("%PDF-1.7\n%%EOF\n");

interface Seen {
  url: string;
  method: string | undefined;
  headers: Headers;
  body: BodyInit | null | undefined;
  redirect: RequestRedirect | undefined;
  cache: RequestCache | undefined;
}

function recorder(response: () => Response) {
  const calls: Seen[] = [];
  const fetchImpl: typeof fetch = async (input, init) => {
    calls.push({
      url: String(input),
      method: init?.method,
      headers: new Headers(init?.headers),
      body: init?.body,
      redirect: init?.redirect,
      cache: init?.cache,
    });
    return response();
  };
  return { calls, fetchImpl };
}

const failing: typeof fetch = async () => {
  throw new TypeError("network");
};

test("workspace: GET with the bearer token, no redirects, no cache", async () => {
  const { calls, fetchImpl } = recorder(() =>
    Response.json({
      id: WORKSPACE,
      name: "Contracts",
      created_at: "2026-09-28T21:00:12+00:00",
    }),
  );
  const result = await getWorkspace(BASE, "tok", TENANT, WORKSPACE, fetchImpl);
  assert.equal(result.state, "ok");
  assert.equal(result.state === "ok" && result.workspace.name, "Contracts");
  assert.equal(calls.length, 1);
  const [call] = calls;
  assert.equal(
    call?.url,
    `${BASE}/v1/tenants/${TENANT}/workspaces/${WORKSPACE}`,
  );
  assert.equal(call?.method, "GET");
  assert.equal(call?.headers.get("authorization"), "Bearer tok");
  assert.equal(call?.redirect, "error");
  assert.equal(call?.cache, "no-store");
});

test("list: parses documents; any malformed entry makes the list unavailable", async () => {
  const ok = recorder(() => Response.json({ documents: [API_DOCUMENT] }));
  assert.deepEqual(
    await listDocuments(BASE, "tok", TENANT, WORKSPACE, ok.fetchImpl),
    { state: "ok", documents: [SUMMARY] },
  );
  assert.equal(
    ok.calls[0]?.url,
    `${BASE}/v1/tenants/${TENANT}/workspaces/${WORKSPACE}/documents`,
  );

  const version = API_DOCUMENT.version;
  const malformed: unknown[] = [
    { documents: "x" },
    {},
    { documents: [{ ...API_DOCUMENT, id: "not-a-uuid" }] },
    { documents: [{ ...API_DOCUMENT, name: 7 }] },
    { documents: [{ ...API_DOCUMENT, created_at: "yesterday" }] },
    {
      documents: [
        { ...API_DOCUMENT, version: { ...version, size_bytes: 1.5 } },
      ],
    },
    {
      documents: [
        { ...API_DOCUMENT, version: { ...version, sha256: SHA.toUpperCase() } },
      ],
    },
    { documents: [{ ...API_DOCUMENT, version: null }] },
  ];
  for (const body of malformed) {
    const { fetchImpl } = recorder(() => Response.json(body));
    assert.deepEqual(
      await listDocuments(BASE, "tok", TENANT, WORKSPACE, fetchImpl),
      { state: "unavailable" },
      JSON.stringify(body),
    );
  }
});

test("status mapping: 404, 401, others and network errors", async () => {
  const cases: [number, string][] = [
    [404, "not_found"],
    [401, "unauthorized"],
    [403, "unavailable"],
    [500, "unavailable"],
  ];
  for (const [status, state] of cases) {
    const { fetchImpl } = recorder(() => new Response(null, { status }));
    assert.equal(
      (await getWorkspace(BASE, "t", TENANT, WORKSPACE, fetchImpl)).state,
      state,
    );
    assert.equal(
      (await listDocuments(BASE, "t", TENANT, WORKSPACE, fetchImpl)).state,
      state,
    );
    assert.equal(
      (
        await downloadDocument(
          BASE,
          "t",
          TENANT,
          WORKSPACE,
          DOCUMENT,
          fetchImpl,
        )
      ).state,
      state,
    );
  }
  assert.equal(
    (await listDocuments(BASE, "t", TENANT, WORKSPACE, failing)).state,
    "unavailable",
  );
  assert.equal(
    (await listDocuments(undefined, "t", TENANT, WORKSPACE)).state,
    "unavailable",
  );
});

test("malformed IDs never reach a URL", async () => {
  const { calls, fetchImpl } = recorder(() => Response.json({}));
  const bad = ["../x", "", TENANT.toUpperCase(), `${TENANT}/..`];
  for (const id of bad) {
    assert.equal(
      (await getWorkspace(BASE, "t", id, WORKSPACE, fetchImpl)).state,
      "not_found",
    );
    assert.equal(
      (await listDocuments(BASE, "t", TENANT, id, fetchImpl)).state,
      "not_found",
    );
    assert.equal(
      (
        await uploadDocument(
          BASE,
          "t",
          id,
          WORKSPACE,
          { bytes: PDF, name: "a", type: "" },
          fetchImpl,
        )
      ).state,
      "not_found",
    );
    assert.equal(
      (await downloadDocument(BASE, "t", TENANT, WORKSPACE, id, fetchImpl))
        .state,
      "not_found",
    );
  }
  assert.equal(calls.length, 0);
});

test("upload: raw bytes with the type and percent-encoded name as hints", async () => {
  const { calls, fetchImpl } = recorder(() =>
    Response.json(API_DOCUMENT, { status: 201 }),
  );
  const result = await uploadDocument(
    BASE,
    "tok",
    TENANT,
    WORKSPACE,
    { bytes: PDF, name: "Årsrapport 2026\n.pdf", type: "application/pdf" },
    fetchImpl,
  );
  assert.deepEqual(result, { state: "uploaded", document: SUMMARY });
  const [call] = calls;
  assert.equal(call?.method, "POST");
  assert.equal(
    call?.url,
    `${BASE}/v1/tenants/${TENANT}/workspaces/${WORKSPACE}/documents`,
  );
  assert.equal(call?.headers.get("authorization"), "Bearer tok");
  assert.equal(call?.headers.get("content-type"), "application/pdf");
  assert.equal(
    call?.headers.get("x-document-name"),
    "%C3%85rsrapport%202026%0A.pdf",
  );
  assert.equal(call?.body, PDF);
  assert.equal(call?.redirect, "error");

  const untyped = recorder(() => Response.json(API_DOCUMENT, { status: 201 }));
  await uploadDocument(
    BASE,
    "tok",
    TENANT,
    WORKSPACE,
    { bytes: PDF, name: "a", type: "" },
    untyped.fetchImpl,
  );
  assert.equal(
    untyped.calls[0]?.headers.get("content-type"),
    "application/octet-stream",
  );
});

test("upload: validation outcomes and unexpected answers", async () => {
  const cases: [Response, string][] = [
    [new Response(null, { status: 413 }), "too_large"],
    [new Response(null, { status: 415 }), "not_pdf"],
    [
      Response.json({ detail: "invalid_name" }, { status: 422 }),
      "invalid_name",
    ],
    // Malware scanning: each API reason maps to its own state.
    [
      Response.json({ detail: "malware_detected" }, { status: 422 }),
      "malware_detected",
    ],
    [
      Response.json({ detail: "scan_incomplete" }, { status: 422 }),
      "scan_incomplete",
    ],
    [
      Response.json({ detail: "encrypted_pdf" }, { status: 422 }),
      "encrypted_pdf",
    ],
    [
      Response.json({ detail: "scan_unavailable" }, { status: 503 }),
      "scan_unavailable",
    ],
    [Response.json({ detail: "unavailable" }, { status: 503 }), "unavailable"],
    [new Response(null, { status: 503 }), "unavailable"],
    // An unknown or missing 422 reason is never reported as an invalid name.
    [
      Response.json({ detail: "something_new" }, { status: 422 }),
      "upload_rejected",
    ],
    [new Response(null, { status: 422 }), "upload_rejected"],
    [new Response("not json", { status: 422 }), "upload_rejected"],
    [new Response(null, { status: 404 }), "not_found"],
    [new Response(null, { status: 401 }), "unauthorized"],
    [Response.json(API_DOCUMENT, { status: 200 }), "unavailable"],
    [Response.json({ id: DOCUMENT }, { status: 201 }), "unavailable"],
    [new Response("not json", { status: 201 }), "unavailable"],
  ];
  for (const [response, state] of cases) {
    const { fetchImpl } = recorder(() => response);
    const result = await uploadDocument(
      BASE,
      "tok",
      TENANT,
      WORKSPACE,
      { bytes: PDF, name: "a.pdf", type: "application/pdf" },
      fetchImpl,
    );
    assert.equal(result.state, state);
  }
  const result = await uploadDocument(
    BASE,
    "tok",
    TENANT,
    WORKSPACE,
    { bytes: PDF, name: "a", type: "" },
    failing,
  );
  assert.equal(result.state, "unavailable");
});

test("download: the content endpoint's response is passed through unread", async () => {
  const upstream = new Response(PDF, {
    headers: { "Content-Disposition": 'attachment; filename="a.pdf"' },
  });
  const { calls, fetchImpl } = recorder(() => upstream);
  const result = await downloadDocument(
    BASE,
    "tok",
    TENANT,
    WORKSPACE,
    DOCUMENT,
    fetchImpl,
  );
  assert.equal(result.state, "ok");
  assert.equal(result.state === "ok" && result.response, upstream);
  assert.equal(upstream.bodyUsed, false);
  assert.equal(
    calls[0]?.url,
    `${BASE}/v1/tenants/${TENANT}/workspaces/${WORKSPACE}/documents/${DOCUMENT}/content`,
  );
  assert.equal(calls[0]?.headers.get("authorization"), "Bearer tok");
});

test("download: the timeout covers the wait for headers, not the streaming", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const signals: AbortSignal[] = [];
  const fetchImpl: typeof fetch = async (_input, init) => {
    if (init?.signal) signals.push(init.signal);
    return new Response(PDF);
  };
  await downloadDocument(BASE, "tok", TENANT, WORKSPACE, DOCUMENT, fetchImpl);
  await listDocuments(BASE, "tok", TENANT, WORKSPACE, fetchImpl);
  t.mock.timers.tick(10 * 60 * 1000);
  assert.equal(signals[0]?.aborted, false, "download stream still open");
  assert.equal(signals[1]?.aborted, true, "metadata calls keep a deadline");
});

test("download: an API that never answers times out as unavailable", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const fetchImpl: typeof fetch = (_input, init) =>
    new Promise((_resolve, reject) =>
      init?.signal?.addEventListener("abort", () =>
        reject(new Error("aborted")),
      ),
    );
  const pending = downloadDocument(
    BASE,
    "tok",
    TENANT,
    WORKSPACE,
    DOCUMENT,
    fetchImpl,
  );
  t.mock.timers.tick(5000);
  assert.equal((await pending).state, "unavailable");
});

test("list: scan status is parsed; a missing or unknown status makes the list unavailable", async () => {
  for (const status of ["clean", "not_scanned", "blocked"] as const) {
    const { fetchImpl } = recorder(() =>
      Response.json({
        documents: [
          {
            ...API_DOCUMENT,
            version: { ...API_DOCUMENT.version, scan_status: status },
          },
        ],
      }),
    );
    const result = await listDocuments(
      BASE,
      "tok",
      TENANT,
      WORKSPACE,
      fetchImpl,
    );
    assert.equal(
      result.state === "ok" && result.documents[0]?.scanStatus,
      status,
    );
  }
  const withoutStatus = Object.fromEntries(
    Object.entries(API_DOCUMENT.version).filter(
      ([key]) => key !== "scan_status",
    ),
  );
  for (const version of [
    withoutStatus,
    { ...API_DOCUMENT.version, scan_status: "maybe" },
  ]) {
    const { fetchImpl } = recorder(() =>
      Response.json({ documents: [{ ...API_DOCUMENT, version }] }),
    );
    assert.deepEqual(
      await listDocuments(BASE, "tok", TENANT, WORKSPACE, fetchImpl),
      {
        state: "unavailable",
      },
    );
  }
});

test("download: 409 maps to not_scanned or blocked (unknown reasons are blocked)", async () => {
  const cases: [Response, string][] = [
    [Response.json({ detail: "not_scanned" }, { status: 409 }), "not_scanned"],
    [Response.json({ detail: "blocked" }, { status: 409 }), "blocked"],
    [Response.json({ detail: "other" }, { status: 409 }), "blocked"],
    [new Response(null, { status: 409 }), "blocked"],
  ];
  for (const [response, state] of cases) {
    const { fetchImpl } = recorder(() => response);
    const result = await downloadDocument(
      BASE,
      "tok",
      TENANT,
      WORKSPACE,
      DOCUMENT,
      fetchImpl,
    );
    assert.equal(result.state, state);
  }
});

test("document: GET one document's metadata; malformed answers are unavailable", async () => {
  const { calls, fetchImpl } = recorder(() => Response.json(API_DOCUMENT));
  assert.deepEqual(
    await getDocument(BASE, "token", TENANT, WORKSPACE, DOCUMENT, fetchImpl),
    { state: "ok", document: SUMMARY },
  );
  assert.equal(
    calls[0]?.url,
    `${BASE}/v1/tenants/${TENANT}/workspaces/${WORKSPACE}/documents/${DOCUMENT}`,
  );
  assert.equal(calls[0]?.headers.get("authorization"), "Bearer token");
  assert.equal(calls[0]?.cache, "no-store");
  const broken = recorder(() => Response.json({ ...API_DOCUMENT, id: "x" }));
  assert.deepEqual(
    await getDocument(BASE, "t", TENANT, WORKSPACE, DOCUMENT, broken.fetchImpl),
    { state: "unavailable" },
  );
  const missing = recorder(() => new Response(null, { status: 404 }));
  assert.deepEqual(
    await getDocument(
      BASE,
      "t",
      TENANT,
      WORKSPACE,
      DOCUMENT,
      missing.fetchImpl,
    ),
    { state: "not_found" },
  );
  const unused = recorder(() => Response.json(API_DOCUMENT));
  assert.deepEqual(
    await getDocument(BASE, "t", TENANT, WORKSPACE, "../x", unused.fetchImpl),
    { state: "not_found" },
  );
  assert.equal(unused.calls.length, 0);
});
