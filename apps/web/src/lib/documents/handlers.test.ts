import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";

import type {
  DownloadResult,
  UploadResult,
  WorkspaceResult,
} from "../api/documents.ts";
import type { Session } from "../auth/session.ts";
import { handleDownload } from "./download-handler.ts";
import {
  DOCUMENT_MAX_BYTES,
  MAX_UPLOAD_BODY_BYTES,
  handleUpload,
  type UploadDeps,
} from "./upload-handler.ts";

const ORIGIN = "https://app.test";
const TENANT = "01a0e9bd-65a9-700c-a38b-c2210bcb6380";
const WORKSPACE = "01a0e9d1-6f65-7ac6-b4b3-84557cd2b17f";
const DOCUMENT = "01a0f000-0000-7000-8000-000000000001";
const PAGE = `${ORIGIN}/tenants/${TENANT}/workspaces/${WORKSPACE}`;
const LOGIN = `${ORIGIN}/auth/login?returnTo=${encodeURIComponent(`/tenants/${TENANT}/workspaces/${WORKSPACE}`)}`;
const NOW = 1_800_000_000;
const SESSION: Session = {
  accessToken: "tok",
  expiresAt: NOW + 600,
  createdAt: NOW - 120,
};
const PDF = new TextEncoder().encode("%PDF-1.7\n% synthetic\n%%EOF\n");

type Uploaded = {
  token: string;
  tenant: string;
  workspace: string;
  name: string;
  type: string;
  sha: string;
};

function uploadDeps(
  result: UploadResult = {
    state: "uploaded",
    document: {
      id: DOCUMENT,
      name: "a.pdf",
      createdAt: "",
      sizeBytes: 1,
      sha256: "0".repeat(64),
      scanStatus: "clean",
    },
  },
  session: Session | null = SESSION,
  workspace: WorkspaceResult = {
    state: "ok",
    workspace: { id: WORKSPACE, name: "Contracts", createdAt: "" },
  },
) {
  const calls: Uploaded[] = [];
  const checks: string[][] = [];
  const logs: string[] = [];
  const deps: UploadDeps = {
    appOrigin: ORIGIN,
    readSession: async () => session,
    upload: async (token, tenant, workspace, file) => {
      calls.push({
        token,
        tenant,
        workspace,
        name: file.name,
        type: file.type,
        sha: createHash("sha256").update(file.bytes).digest("hex"),
      });
      return result;
    },
    workspace: async (token, tenant, workspaceId) => {
      checks.push([token, tenant, workspaceId]);
      return workspace;
    },
    now: () => NOW,
    log: (message) => logs.push(message),
  };
  return { calls, checks, logs, deps };
}

function multipart(
  files: { name: string; type: string; bytes: Uint8Array<ArrayBuffer> }[],
  origin: string | null = ORIGIN,
): Request {
  const form = new FormData();
  for (const file of files)
    form.append("file", new File([file.bytes], file.name, { type: file.type }));
  const headers: Record<string, string> =
    origin === null ? {} : { Origin: origin };
  return new Request(`${PAGE}/documents/upload`, {
    method: "POST",
    headers,
    body: form,
  });
}

function assertRedirect(response: Response, location: string) {
  assert.equal(response.status, 303);
  assert.equal(response.headers.get("location"), location);
  assert.equal(response.headers.get("cache-control"), "no-store");
}

// --- upload ---------------------------------------------------------------------------------

test("a PDF is forwarded byte-identical with its name and declared type as hints", async () => {
  const { calls, deps } = uploadDeps();
  const response = await handleUpload(
    multipart([
      { name: "Årsrapport.pdf", type: "application/pdf", bytes: PDF },
    ]),
    TENANT,
    WORKSPACE,
    deps,
  );
  assertRedirect(response, `${PAGE}?uploaded=1`);
  assert.deepEqual(calls, [
    {
      token: "tok",
      tenant: TENANT,
      workspace: WORKSPACE,
      name: "Årsrapport.pdf",
      type: "application/pdf",
      sha: createHash("sha256").update(PDF).digest("hex"),
    },
  ]);
});

for (const origin of [null, "https://evil.test", "null", `${ORIGIN}/`]) {
  test(`upload with Origin ${String(origin)} is refused before reading anything`, async () => {
    const { calls, logs, deps } = uploadDeps();
    const response = await handleUpload(
      multipart(
        [{ name: "a.pdf", type: "application/pdf", bytes: PDF }],
        origin,
      ),
      TENANT,
      WORKSPACE,
      deps,
    );
    assert.equal(response.status, 403);
    assert.equal(calls.length, 0);
    assert.deepEqual(logs, ["upload refused: origin_mismatch"]);
  });
}

test("malformed IDs are 404 and configuration errors 503, with no API call", async () => {
  const { calls, deps } = uploadDeps();
  for (const [tenant, workspace] of [
    ["not-a-uuid", WORKSPACE],
    [TENANT, WORKSPACE.toUpperCase()],
  ]) {
    const response = await handleUpload(
      multipart([{ name: "a.pdf", type: "application/pdf", bytes: PDF }]),
      tenant ?? "",
      workspace ?? "",
      deps,
    );
    assert.equal(response.status, 404);
  }
  const unconfigured = await handleUpload(
    multipart([{ name: "a.pdf", type: "application/pdf", bytes: PDF }]),
    TENANT,
    WORKSPACE,
    { ...deps, appOrigin: null },
  );
  assert.equal(unconfigured.status, 503);
  assert.equal(calls.length, 0);
});

test("without a session the user is sent to sign in, returning to the workspace", async () => {
  const { calls, deps } = uploadDeps(undefined, null);
  assertRedirect(
    await handleUpload(
      multipart([{ name: "a.pdf", type: "application/pdf", bytes: PDF }]),
      TENANT,
      WORKSPACE,
      deps,
    ),
    LOGIN,
  );
  assert.equal(calls.length, 0);
});

test("a file of exactly the limit is forwarded; one byte more is refused locally", async () => {
  const atLimit = new Uint8Array(DOCUMENT_MAX_BYTES);
  atLimit.set(PDF);
  const ok = uploadDeps();
  assertRedirect(
    await handleUpload(
      multipart([{ name: "big.pdf", type: "application/pdf", bytes: atLimit }]),
      TENANT,
      WORKSPACE,
      ok.deps,
    ),
    `${PAGE}?uploaded=1`,
  );
  assert.equal(ok.calls.length, 1);

  const over = uploadDeps();
  const tooBig = new Uint8Array(DOCUMENT_MAX_BYTES + 1);
  assertRedirect(
    await handleUpload(
      multipart([{ name: "big.pdf", type: "application/pdf", bytes: tooBig }]),
      TENANT,
      WORKSPACE,
      over.deps,
    ),
    `${PAGE}?error=too_large`,
  );
  assert.equal(over.calls.length, 0);
});

test("a declared body over the limit is refused without reading it", async () => {
  const { calls, deps } = uploadDeps();
  let pulled = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      pulled += 1;
      controller.enqueue(new Uint8Array(1024));
    },
  });
  const request = new Request(`${PAGE}/documents/upload`, {
    method: "POST",
    headers: {
      Origin: ORIGIN,
      "Content-Type": "multipart/form-data; boundary=x",
      "Content-Length": String(MAX_UPLOAD_BODY_BYTES + 1),
    },
    body,
    duplex: "half",
  } as RequestInit);
  assertRedirect(
    await handleUpload(request, TENANT, WORKSPACE, deps),
    `${PAGE}?error=too_large`,
  );
  assert.equal(calls.length, 0);
  assert.ok(pulled <= 1, `read ${pulled} chunks`);
});

test("a streamed body over the limit stops being read", async () => {
  const { calls, deps } = uploadDeps();
  let pulled = 0;
  const chunk = new Uint8Array(1 << 20);
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      pulled += 1;
      controller.enqueue(chunk);
    },
  });
  const request = new Request(`${PAGE}/documents/upload`, {
    method: "POST",
    headers: {
      Origin: ORIGIN,
      "Content-Type": "multipart/form-data; boundary=x",
    },
    body,
    duplex: "half",
  } as RequestInit);
  assertRedirect(
    await handleUpload(request, TENANT, WORKSPACE, deps),
    `${PAGE}?error=too_large`,
  );
  assert.equal(calls.length, 0);
  assert.ok(
    pulled <= Math.ceil(MAX_UPLOAD_BODY_BYTES / chunk.byteLength) + 2,
    `read ${pulled} chunks`,
  );
});

test("missing, empty, repeated or non-file fields are handled without an API call", async () => {
  const cases: [Request, number, string | null][] = [
    [multipart([]), 303, `${PAGE}?error=missing_file`],
    [
      multipart([
        {
          name: "empty.pdf",
          type: "application/pdf",
          bytes: new Uint8Array(0),
        },
      ]),
      303,
      `${PAGE}?error=missing_file`,
    ],
    [
      multipart([
        { name: "a.pdf", type: "application/pdf", bytes: PDF },
        { name: "b.pdf", type: "application/pdf", bytes: PDF },
      ]),
      400,
      null,
    ],
    [
      new Request(`${PAGE}/documents/upload`, {
        method: "POST",
        headers: { Origin: ORIGIN, "Content-Type": "application/pdf" },
        body: PDF,
      }),
      400,
      null,
    ],
    [
      new Request(`${PAGE}/documents/upload`, {
        method: "POST",
        headers: {
          Origin: ORIGIN,
          "Content-Type": "multipart/form-data; boundary=x",
        },
        body: "garbage",
      }),
      400,
      null,
    ],
  ];
  for (const [request, status, location] of cases) {
    const { calls, deps } = uploadDeps();
    const response = await handleUpload(request, TENANT, WORKSPACE, deps);
    assert.equal(response.status, status);
    if (location) assert.equal(response.headers.get("location"), location);
    assert.equal(calls.length, 0);
  }
});

test("API outcomes map to fixed codes; nothing submitted is echoed", async () => {
  const cases: [UploadResult, string][] = [
    [{ state: "too_large" }, `${PAGE}?error=too_large`],
    [{ state: "not_pdf" }, `${PAGE}?error=not_pdf`],
    [{ state: "invalid_name" }, `${PAGE}?error=invalid_name`],
    [{ state: "not_found" }, PAGE],
    [{ state: "unavailable" }, `${PAGE}?error=unavailable`],
    [{ state: "unauthorized" }, LOGIN],
    // Malware scanning outcomes: each has its own code, never invalid_name.
    [{ state: "malware_detected" }, `${PAGE}?error=malware_detected`],
    [{ state: "scan_incomplete" }, `${PAGE}?error=scan_incomplete`],
    [{ state: "encrypted_pdf" }, `${PAGE}?error=encrypted_pdf`],
    [{ state: "scan_unavailable" }, `${PAGE}?error=scan_unavailable`],
    [{ state: "upload_rejected" }, `${PAGE}?error=upload_rejected`],
  ];
  for (const [result, location] of cases) {
    const { deps } = uploadDeps(result);
    const response = await handleUpload(
      multipart([
        { name: "<script>x</script>.pdf", type: "text/html", bytes: PDF },
      ]),
      TENANT,
      WORKSPACE,
      deps,
    );
    assertRedirect(response, location);
    assert.ok(!(response.headers.get("location") ?? "").includes("script"));
  }
  const fresh = uploadDeps(
    { state: "unauthorized" },
    { ...SESSION, createdAt: NOW - 5 },
  );
  assertRedirect(
    await handleUpload(
      multipart([{ name: "a.pdf", type: "application/pdf", bytes: PDF }]),
      TENANT,
      WORKSPACE,
      fresh.deps,
    ),
    `${PAGE}?error=rejected`,
  );
});

test("the workspace is checked with the session's token before the upload", async () => {
  const { calls, checks, deps } = uploadDeps();
  await handleUpload(
    multipart([{ name: "a.pdf", type: "application/pdf", bytes: PDF }]),
    TENANT,
    WORKSPACE,
    deps,
  );
  assert.deepEqual(checks, [["tok", TENANT, WORKSPACE]]);
  assert.equal(calls.length, 1);
});

// A request whose body counts every pull: 0 proves nothing of it was read.
function countingUpload(): { request: Request; pulls: () => number } {
  let pulls = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      pulls += 1;
      if (pulls > 64) controller.close();
      else controller.enqueue(new Uint8Array(64 * 1024));
    },
  });
  const request = new Request(`${PAGE}/documents/upload`, {
    method: "POST",
    headers: {
      Origin: ORIGIN,
      "Content-Type": "multipart/form-data; boundary=x",
    },
    body,
    duplex: "half",
  } as RequestInit);
  return { request, pulls: () => pulls };
}

test("non-members, unknown workspaces and API failures are refused before any body read (D-058)", async () => {
  const cases: [WorkspaceResult, Session, string][] = [
    [{ state: "not_found" }, SESSION, PAGE],
    [{ state: "unauthorized" }, SESSION, LOGIN],
    [
      { state: "unauthorized" },
      { ...SESSION, createdAt: NOW - 5 },
      `${PAGE}?error=rejected`,
    ],
    [{ state: "unavailable" }, SESSION, `${PAGE}?error=unavailable`],
  ];
  for (const [workspace, session, location] of cases) {
    const { calls, checks, deps } = uploadDeps(undefined, session, workspace);
    const { request, pulls } = countingUpload();
    // A stream pre-fills one chunk by itself shortly after creation; take the
    // baseline after that.
    await new Promise((resolve) => setTimeout(resolve, 0));
    const before = pulls();
    const response = await handleUpload(request, TENANT, WORKSPACE, deps);
    assertRedirect(response, location);
    assert.equal(checks.length, 1, workspace.state);
    assert.equal(calls.length, 0, workspace.state);
    assert.equal(pulls(), before, `${workspace.state}: body pulled`);
    // Reading needs a reader, which locks the stream; it was never taken.
    assert.equal(request.body?.locked, false, workspace.state);
    assert.equal(request.bodyUsed, false, workspace.state);
  }
});

test("an invisible workspace is refused before any body validation (intended precedence since D-058)", async () => {
  // Oversized (declared) and non-multipart bodies: with a visible workspace
  // they get too_large and 400; with an invisible one the uniform 404 page,
  // decided before the body is touched.
  const invalid: [string, Record<string, string>, number, string | null][] = [
    [
      "declared oversize",
      {
        "Content-Type": "multipart/form-data; boundary=x",
        "Content-Length": String(MAX_UPLOAD_BODY_BYTES + 1),
      },
      303,
      `${PAGE}?error=too_large`,
    ],
    ["not multipart", { "Content-Type": "application/pdf" }, 400, null],
  ];
  for (const [label, headers, status, location] of invalid) {
    const request = () =>
      new Request(`${PAGE}/documents/upload`, {
        method: "POST",
        headers: { Origin: ORIGIN, ...headers },
        body: new ReadableStream<Uint8Array>({
          pull(controller) {
            controller.enqueue(new Uint8Array(1024));
            controller.close();
          },
        }),
        duplex: "half",
      } as RequestInit);
    const hidden = uploadDeps(undefined, SESSION, { state: "not_found" });
    const refused = request();
    assertRedirect(
      await handleUpload(refused, TENANT, WORKSPACE, hidden.deps),
      PAGE,
    );
    assert.equal(refused.bodyUsed, false, label);
    assert.equal(refused.body?.locked, false, label);
    assert.equal(hidden.calls.length, 0, label);
    // Control: the same request to a visible workspace keeps its own answer.
    const visible = uploadDeps();
    const response = await handleUpload(
      request(),
      TENANT,
      WORKSPACE,
      visible.deps,
    );
    assert.equal(response.status, status, label);
    assert.equal(response.headers.get("location"), location, label);
  }
});

test("positive control: an authorized upload reads the body", async () => {
  const { deps } = uploadDeps();
  const { request, pulls } = countingUpload();
  await new Promise((resolve) => setTimeout(resolve, 0));
  const before = pulls();
  await handleUpload(request, TENANT, WORKSPACE, deps);
  assert.ok(pulls() > before);
  assert.equal(request.bodyUsed, true);
});

test("no session and bad Origin are refused before the workspace check", async () => {
  const noSession = uploadDeps(undefined, null);
  await handleUpload(
    multipart([{ name: "a.pdf", type: "application/pdf", bytes: PDF }]),
    TENANT,
    WORKSPACE,
    noSession.deps,
  );
  const foreign = uploadDeps();
  await handleUpload(
    multipart(
      [{ name: "a.pdf", type: "application/pdf", bytes: PDF }],
      "https://evil.test",
    ),
    TENANT,
    WORKSPACE,
    foreign.deps,
  );
  assert.equal(noSession.checks.length + foreign.checks.length, 0);
});

// --- download -------------------------------------------------------------------------------

function downloadDeps(
  result: DownloadResult,
  session: Session | null = SESSION,
) {
  const calls: string[][] = [];
  const logs: string[] = [];
  return {
    calls,
    logs,
    deps: {
      readSession: async () => session,
      download: async (
        token: string,
        tenant: string,
        workspace: string,
        document: string,
      ) => {
        calls.push([token, tenant, workspace, document]);
        return result;
      },
      now: () => NOW,
      log: (message: string) => logs.push(message),
    },
  };
}

function apiFile(
  disposition = "attachment; filename=\"a.pdf\"; filename*=UTF-8''a.pdf",
): Response {
  return new Response(PDF, {
    headers: {
      "Content-Type": "application/pdf",
      "Content-Length": String(PDF.byteLength),
      "Content-Disposition": disposition,
    },
  });
}

test("a download streams the API's bytes as a sandboxed attachment", async () => {
  const { calls, deps } = downloadDeps({ state: "ok", response: apiFile() });
  const response = await handleDownload(TENANT, WORKSPACE, DOCUMENT, deps);
  assert.equal(response.status, 200);
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), PDF);
  assert.equal(response.headers.get("content-type"), "application/pdf");
  assert.equal(
    response.headers.get("content-disposition"),
    "attachment; filename=\"a.pdf\"; filename*=UTF-8''a.pdf",
  );
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  assert.equal(
    response.headers.get("content-security-policy"),
    "sandbox; default-src 'none'",
  );
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(response.headers.get("content-length"), String(PDF.byteLength));
  assert.deepEqual(calls, [["tok", TENANT, WORKSPACE, DOCUMENT]]);
});

test("an upstream response that is not an attachment is never passed on", async () => {
  const { deps } = downloadDeps({ state: "ok", response: apiFile("inline") });
  assert.equal(
    (await handleDownload(TENANT, WORKSPACE, DOCUMENT, deps)).status,
    502,
  );
});

test("a download of a version that is not clean goes back to the page with a fixed code", async () => {
  for (const state of ["not_scanned", "blocked"] as const) {
    const { deps } = downloadDeps({ state });
    const response = await handleDownload(TENANT, WORKSPACE, DOCUMENT, deps);
    assert.equal(response.status, 303);
    assert.equal(
      response.headers.get("location"),
      `/tenants/${TENANT}/workspaces/${WORKSPACE}?error=${state}`,
    );
    assert.equal(response.headers.get("cache-control"), "no-store");
  }
});

test("download failures: 404, sign-in, rejected, unavailable; malformed IDs make no call", async () => {
  const notFound = downloadDeps({ state: "not_found" });
  assert.equal(
    (await handleDownload(TENANT, WORKSPACE, DOCUMENT, notFound.deps)).status,
    404,
  );

  const unavailable = downloadDeps({ state: "unavailable" });
  assert.equal(
    (await handleDownload(TENANT, WORKSPACE, DOCUMENT, unavailable.deps))
      .status,
    503,
  );

  const noSession = downloadDeps({ state: "ok", response: apiFile() }, null);
  const login = await handleDownload(
    TENANT,
    WORKSPACE,
    DOCUMENT,
    noSession.deps,
  );
  assert.equal(login.status, 303);
  assert.equal(
    login.headers.get("location"),
    `/auth/login?returnTo=${encodeURIComponent(`/tenants/${TENANT}/workspaces/${WORKSPACE}`)}`,
  );
  assert.equal(noSession.calls.length, 0);

  const old = downloadDeps({ state: "unauthorized" });
  assert.match(
    (await handleDownload(TENANT, WORKSPACE, DOCUMENT, old.deps)).headers.get(
      "location",
    ) ?? "",
    /^\/auth\/login/,
  );
  const fresh = downloadDeps(
    { state: "unauthorized" },
    { ...SESSION, createdAt: NOW - 5 },
  );
  assert.equal(
    (await handleDownload(TENANT, WORKSPACE, DOCUMENT, fresh.deps)).headers.get(
      "location",
    ),
    `/tenants/${TENANT}/workspaces/${WORKSPACE}?error=rejected`,
  );

  const malformed = downloadDeps({ state: "ok", response: apiFile() });
  for (const ids of [
    ["x", WORKSPACE, DOCUMENT],
    [TENANT, "x", DOCUMENT],
    [TENANT, WORKSPACE, DOCUMENT.toUpperCase()],
  ]) {
    assert.equal(
      (
        await handleDownload(
          ids[0] ?? "",
          ids[1] ?? "",
          ids[2] ?? "",
          malformed.deps,
        )
      ).status,
      404,
    );
  }
  assert.equal(malformed.calls.length, 0);
});
