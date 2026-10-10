// After `next build`: start the production server (`next start`) on a free local
// port with test-only configuration and check the authentication wiring end to
// end: headers from next.config, route methods, Server Component redirects, and
// that every session invalidation goes through a route handler response; and the
// tenant workspace pages and create route (states, status codes, Origin checks,
// escaping, accessibility markup); and the workspace document page, upload and
// download routes (bodies over the proxy's 10 MB buffer arrive intact, limits,
// byte-identical downloads, attachment headers).
//
// No real identity provider is contacted: OIDC_ISSUER points at an unresolvable
// .invalid host, so login answers 503. A local stub stands in for the API.
import { spawn } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { createServer } from "node:http";
import { once } from "node:events";
import { join } from "node:path";

import { sealData } from "iron-session";

const APP_ORIGIN = "https://localhost:3000";
const SECRET = randomBytes(32).toString("hex");
const failures = [];
const check = (label, condition, detail = "") => {
  console.log(
    `  [${condition ? "PASS" : "FAIL"}] ${label}${condition ? "" : ` ${detail}`}`,
  );
  if (!condition) failures.push(label);
};

// --- API stub: /v1/me and the tenant workspace endpoints, per bearer token ---
const TENANT = "01a0e9bd-65a9-700c-a38b-c2210bcb6380"; // member, one workspace
const EMPTY_TENANT = "01a0e9bd-65a9-700c-a38b-c2210bcb6381"; // member, no workspaces
const DOWN_TENANT = "01a0e9bd-65a9-700c-a38b-c2210bcb6382"; // API answers 503
const WORKSPACE = {
  id: "01a0e9d1-6f65-7ac6-b4b3-84557cd2b17f",
  name: "<b>Contracts</b> & co",
  created_at: "2026-09-28T21:00:12.003958+00:00",
};
const DOCUMENT_MAX_BYTES = 20 * 1024 * 1024;
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
// Documents of WORKSPACE, newest first. The first is pre-seeded so the list and
// download render without an upload.
const SEEDED_PDF = Buffer.from(
  "%PDF-1.7\n% seeded synthetic document\n%%EOF\n",
);
const documents = [
  {
    id: "01a0f000-0000-7000-8000-000000000001",
    name: "<i>Seeded</i> report.pdf",
    created_at: "2026-09-29T08:00:00.000000+00:00",
    bytes: SEEDED_PDF,
  },
];
// A second workspace whose documents are not clean (scan status; 409 on download).
const SCAN_WORKSPACE = {
  id: "01a0e9d1-6f65-7ac6-b4b3-84557cd2b18f",
  name: "Scan states",
  created_at: "2026-09-30T08:00:00.000000+00:00",
};
const scanDocuments = [
  {
    id: "01a0f000-0000-7000-8000-00000000a001",
    name: "never-scanned.pdf",
    created_at: "2026-09-29T07:00:00.000000+00:00",
    bytes: SEEDED_PDF,
    scan: "not_scanned",
  },
  {
    id: "01a0f000-0000-7000-8000-00000000a002",
    name: "blocked.pdf",
    created_at: "2026-09-29T06:00:00.000000+00:00",
    bytes: SEEDED_PDF,
    scan: "blocked",
  },
];
// DI-1: a workspace with one document per processing state (GET/POST
// .../documents/{d}/processing), including an API outage and an answer that
// does not match the contract.
const PROC_WORKSPACE = {
  id: "01a0e9d1-6f65-7ac6-b4b3-84557cd2b19f",
  name: "Processing states",
  created_at: "2026-09-30T09:00:00.000000+00:00",
};
const PIPELINE = `service_agreement.v1+rules-1+parser@sha256:${"ab".repeat(32)}`;
const apiRun = (status, extra = {}) => ({
  id: "01a0f000-0000-7000-8000-0000000000c1",
  status,
  failure_code: null,
  attempts: 1,
  pipeline_version: PIPELINE,
  created_at: "2026-09-30T10:00:00.000000+00:00",
  started_at: "2026-09-30T10:00:01.000000+00:00",
  finished_at: null,
  ...extra,
});
const span = (quote, page = 1) => ({
  page,
  start: 0,
  end: quote.length,
  quote,
});
const foundField = (value, raw, ...quotes) => ({
  status: "found",
  reason: null,
  candidates: [{ value, raw, evidence: quotes }],
});
const NOT_FOUND = { status: "unknown", reason: "not_found", candidates: [] };
const COMPLETED_RESULT = {
  schema: "service_agreement.v1",
  extractor: "rules-1",
  outcome: "completed",
  fields: {
    provider_name: foundField(
      "Northwind Analytics Ltd",
      "Northwind Analytics Ltd",
      span("Provider: Northwind Analytics Ltd"),
    ),
    customer_name: foundField(
      "Harbour Freight Services Ltd",
      "Harbour Freight Services Ltd",
      span("Customer: Harbour Freight Services Ltd"),
    ),
    effective_date: foundField(
      "2026-03-01",
      "1 March 2026",
      span("Effective Date: 1 March 2026"),
    ),
    end_date: NOT_FOUND,
    governing_law: foundField(
      "<b>Law</b> & order",
      "<b>Law</b> & order",
      span("Governing Law: <b>Law</b> & order"),
    ),
    contract_value: foundField(
      { amount: "120000.00", currency: "EUR" },
      "EUR 120,000.00",
      span("Contract Value: EUR 120,000.00", 2),
    ),
    notice_period_days: foundField(
      90,
      "90 days",
      span("Notice Period: 90 days", 2),
    ),
  },
};
const REVIEW_RESULT = {
  schema: "service_agreement.v1",
  extractor: "rules-1",
  outcome: "needs_review",
  fields: {
    provider_name: foundField(
      "Northwind Analytics Ltd",
      "Northwind Analytics Ltd",
      span("Provider: Northwind Analytics Ltd"),
      span("between Northwind Analytics Ltd and"),
    ),
    customer_name: NOT_FOUND,
    effective_date: {
      status: "conflict",
      reason: null,
      candidates: [
        {
          value: "2026-03-01",
          raw: "1 March 2026",
          evidence: [span("Effective Date: 1 March 2026")],
        },
        {
          value: "2026-04-01",
          raw: "1 April 2026",
          evidence: [span("effective as of 1 April 2026", 2)],
        },
      ],
    },
    end_date: {
      status: "unknown",
      reason: "ambiguous",
      candidates: [
        {
          value: null,
          raw: "03/04/2027",
          evidence: [span("End Date: 03/04/2027")],
        },
      ],
    },
    governing_law: NOT_FOUND,
    contract_value: NOT_FOUND,
    notice_period_days: {
      status: "unknown",
      reason: "unparseable",
      candidates: [
        {
          value: null,
          raw: "<script>alert(1)</script>",
          evidence: [span("Notice Period: <script>alert(1)</script>")],
        },
      ],
    },
  },
};
const PROCESSING = {
  never: { run: null, result: null },
  queued: {
    run: apiRun("queued", { attempts: 0, started_at: null }),
    result: null,
  },
  running: { run: apiRun("running"), result: null },
  completed: {
    run: apiRun("completed", {
      finished_at: "2026-09-30T10:00:03.000000+00:00",
    }),
    result: COMPLETED_RESULT,
  },
  needs_review: {
    run: apiRun("needs_review", {
      finished_at: "2026-09-30T10:00:03.000000+00:00",
    }),
    result: REVIEW_RESULT,
  },
  failed: {
    run: apiRun("failed", {
      failure_code: "embedded_content",
      finished_at: "2026-09-30T10:00:03.000000+00:00",
    }),
    result: null,
  },
  malformed: {
    run: apiRun("completed", {
      finished_at: "2026-09-30T10:00:03.000000+00:00",
    }),
    result: { ...COMPLETED_RESULT, schema: "other" },
  },
};
// R1 review states (GET …/review) per document state; anything else: nothing to review.
const RUN_ID = apiRun("needs_review").id;
const OTHER_RUN_ID = "01a0f000-0000-7000-8000-0000000000c2";
const REVIEW_AT = "2026-10-01T09:00:00.000000Z";
const decisionRow = (seq, fieldKey, ordinal) => ({
  seq,
  field_key: fieldKey,
  resolution: ordinal === null ? "unknown" : "candidate",
  candidate_ordinal: ordinal,
  decided_by: "01a0f000-0000-7000-8000-0000000000d1",
  decided_at: REVIEW_AT,
});
const approvalRow = (version, decisionsThrough) => ({
  version,
  run_id: RUN_ID,
  decisions_through: decisionsThrough,
  approved_by: "01a0f000-0000-7000-8000-0000000000d1",
  approved_at: REVIEW_AT,
  record_sha256: "c".repeat(64),
});
const reviewState = (decisions = [], approvals = [], extra = {}) => ({
  run_id: RUN_ID,
  can_review: true,
  decisions_through: decisions.length,
  decisions,
  approvals,
  ...extra,
});
const RESOLVED = [
  decisionRow(1, "effective_date", 1),
  decisionRow(2, "customer_name", null),
];
const REVIEW = {
  completed: reviewState(),
  needs_review: reviewState(),
  reviewed: reviewState(RESOLVED, [approvalRow(1, 2)]),
  changed: reviewState(
    [...RESOLVED, decisionRow(3, "effective_date", 0)],
    [approvalRow(1, 2)],
  ),
  member: reviewState([decisionRow(1, "effective_date", 1)], [], {
    can_review: false,
  }),
  other_run: reviewState([], [], { run_id: OTHER_RUN_ID }),
};
const NOTHING_TO_REVIEW = reviewState([], [], { run_id: null });
for (const state of [
  "reviewed",
  "changed",
  "member",
  "other_run",
  "review_unavailable",
]) {
  PROCESSING[state] = PROCESSING.needs_review;
}
// The approved record the export serves (bytes passed through unchanged).
const RECORD_BYTES = Buffer.from(
  '{"approval":{"version":1},"schema":"approved_record.v1","note":"<b>escaped</b>"}',
);
const reviewPosts = [];

const procDocuments = [
  "never",
  "queued",
  "running",
  "completed",
  "needs_review",
  "failed",
  "malformed",
  "unavailable",
  "start_unavailable",
  "reviewed",
  "changed",
  "member",
  "other_run",
  "review_unavailable",
].map((state, index) => ({
  id: `01a0f000-0000-7000-8000-00000000b0${String(index + 10)}`,
  name: `${state}.pdf`,
  created_at: "2026-09-30T08:00:00.000000+00:00",
  bytes: SEEDED_PDF,
  state,
}));
const processingPosts = [];

// Upload bodies containing these markers get the API's scan outcomes.
const MALWARE_MARKER = "SIMULATED-MALWARE-MARKER";
const SCANNER_DOWN_MARKER = "SIMULATED-SCANNER-DOWN";
const apiDocument = (doc) => ({
  id: doc.id,
  workspace_id: WORKSPACE.id,
  name: doc.name,
  created_at: doc.created_at,
  version: {
    id: "01a0f000-0000-7000-8000-00000000ffff",
    number: 1,
    media_type: "application/pdf",
    size_bytes: doc.bytes.byteLength,
    sha256: sha256(doc.bytes),
    scan_status: doc.scan ?? "clean",
  },
});
// The API's review endpoints for the processing-states workspace (R1).
function fakeReview(request, reply, response, doc, suffix, body) {
  if (suffix === "/review") {
    if (doc.state === "review_unavailable") {
      return reply(503, { detail: "unavailable" });
    }
    return reply(200, REVIEW[doc.state] ?? NOTHING_TO_REVIEW);
  }
  const exported = /^\/review\/approvals\/([^/]+)\/export$/.exec(suffix);
  if (exported) {
    if (doc.state !== "reviewed" || exported[1] !== "1") {
      return reply(404, { detail: "not_found" });
    }
    return response
      .writeHead(200, {
        "Content-Type": "application/json",
        "Content-Length": String(RECORD_BYTES.byteLength),
        "Content-Disposition":
          "attachment; filename=\"reviewed-approved-v1.json\"; filename*=UTF-8''reviewed-approved-v1.json",
        "X-Record-SHA256": sha256(RECORD_BYTES),
      })
      .end(RECORD_BYTES);
  }
  if (request.method !== "POST") return reply(405, { detail: "not_found" });
  let payload = null;
  try {
    payload = JSON.parse(body);
  } catch {
    return reply(422, { detail: "invalid_request" });
  }
  reviewPosts.push({ state: doc.state, suffix, payload });
  if (doc.state === "member") return reply(403, { detail: "not_reviewer" });
  if (payload.expected_seq === 99) return reply(409, { detail: "stale" });
  if (suffix === "/review/decisions") {
    if (payload.field_key === "end_date") {
      return reply(422, { detail: "invalid_decision" });
    }
    return reply(
      201,
      decisionRow(
        payload.expected_seq + 1,
        payload.field_key,
        payload.candidate_ordinal ?? null,
      ),
    );
  }
  if (doc.state === "needs_review") {
    return reply(422, {
      detail: "unresolved_fields",
      fields: ["customer_name", "effective_date"],
    });
  }
  return reply(
    201,
    approvalRow(payload.expected_version + 1, payload.expected_seq),
  );
}

const apiCalls = [];
const api = createServer(async (request, response) => {
  const auth = request.headers.authorization ?? "";
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const raw = Buffer.concat(chunks);
  const body = raw.toString();
  apiCalls.push({
    method: request.method,
    url: request.url,
    body: raw.byteLength > 4096 ? "" : body,
    size: raw.byteLength,
    sha256: sha256(raw),
    headers: request.headers,
  });
  const reply = (status, payload) =>
    response
      .writeHead(status, { "Content-Type": "application/json" })
      .end(JSON.stringify(payload));
  if (auth !== "Bearer good-token")
    return reply(401, { detail: "unauthorized" });
  if (request.url === "/v1/me") {
    return reply(200, {
      user_id: "u-1",
      memberships: [{ tenant_id: TENANT, role: "owner" }],
    });
  }
  const inWorkspace =
    /^\/v1\/tenants\/([^/]+)\/workspaces\/([^/]+)(\/documents(?:\/([^/]+)(\/content|\/processing|\/review(?:\/decisions|\/approvals(?:\/[^/]+\/export)?)?)?)?)?$/.exec(
      request.url ?? "",
    );
  if (inWorkspace) {
    const [, tenant, workspace, docs, documentId, suffix] = inWorkspace;
    if (tenant === TENANT && workspace === PROC_WORKSPACE.id) {
      if (!docs) return reply(200, PROC_WORKSPACE);
      if (!documentId) {
        return reply(200, { documents: procDocuments.map(apiDocument) });
      }
      const doc = procDocuments.find((d) => d.id === documentId);
      if (!doc) return reply(404, { detail: "not_found" });
      if (!suffix) return reply(200, apiDocument(doc));
      if (suffix.startsWith("/review")) {
        return fakeReview(request, reply, response, doc, suffix, body);
      }
      if (suffix !== "/processing") return reply(404, { detail: "not_found" });
      if (request.method === "POST") {
        processingPosts.push(doc.state);
        if (doc.state === "start_unavailable") {
          return reply(503, { detail: "processing_unavailable" });
        }
        return reply(doc.state === "never" ? 202 : 200, {
          run:
            PROCESSING[doc.state]?.run ??
            apiRun("queued", { attempts: 0, started_at: null }),
        });
      }
      if (doc.state === "unavailable")
        return reply(503, { detail: "unavailable" });
      return reply(200, PROCESSING[doc.state] ?? PROCESSING.never);
    }
    if (tenant === TENANT && workspace === SCAN_WORKSPACE.id) {
      if (!docs) return reply(200, SCAN_WORKSPACE);
      if (!documentId && request.method === "GET") {
        return reply(200, { documents: scanDocuments.map(apiDocument) });
      }
      const doc = scanDocuments.find((d) => d.id === documentId);
      if (!doc) return reply(404, { detail: "not_found" });
      if (!suffix) return reply(200, apiDocument(doc));
      return reply(409, { detail: doc.scan });
    }
    if (tenant !== TENANT || workspace !== WORKSPACE.id) {
      return reply(404, { detail: "not_found" });
    }
    if (!docs) return reply(200, WORKSPACE);
    if (documentId) {
      const doc = documents.find((d) => d.id === documentId);
      if (!doc) return reply(404, { detail: "not_found" });
      if (!suffix) return reply(200, apiDocument(doc));
      if (suffix.startsWith("/review")) return reply(200, NOTHING_TO_REVIEW);
      if (suffix === "/processing") {
        if (request.method === "POST") {
          processingPosts.push("seeded");
          return reply(202, {
            run: apiRun("queued", { attempts: 0, started_at: null }),
          });
        }
        return reply(200, PROCESSING.never);
      }
      return response
        .writeHead(200, {
          "Content-Type": "application/pdf",
          "Content-Length": String(doc.bytes.byteLength),
          "Content-Disposition": `attachment; filename="document.pdf"; filename*=UTF-8''${encodeURIComponent(doc.name)}`,
        })
        .end(doc.bytes);
    }
    if (request.method === "GET") {
      return reply(200, { documents: documents.map(apiDocument) });
    }
    // Same order and codes as the API: size, signature, name.
    if (raw.byteLength > DOCUMENT_MAX_BYTES) {
      return reply(413, { detail: "too_large" });
    }
    if (!raw.subarray(0, 5).equals(Buffer.from("%PDF-"))) {
      return reply(415, { detail: "unsupported_media_type" });
    }
    const name = decodeURIComponent(request.headers["x-document-name"] ?? "");
    if (!name || name.length > 255)
      return reply(422, { detail: "invalid_name" });
    // The malware scan (the API's decision; nothing stored unless clean).
    if (raw.includes(MALWARE_MARKER)) {
      return reply(422, { detail: "malware_detected" });
    }
    if (raw.includes(SCANNER_DOWN_MARKER)) {
      return reply(503, { detail: "scan_unavailable" });
    }
    const doc = {
      id: `01a0f000-0000-7000-8000-${String(documents.length + 1).padStart(12, "0")}`,
      name,
      created_at: new Date().toISOString(),
      bytes: raw,
    };
    documents.unshift(doc);
    return reply(201, apiDocument(doc));
  }
  const match = /^\/v1\/tenants\/([^/]+)\/workspaces$/.exec(request.url ?? "");
  if (!match) return reply(404, { detail: "not_found" });
  const tenant = match[1];
  if (tenant === DOWN_TENANT) return reply(503, { detail: "unavailable" });
  if (tenant !== TENANT && tenant !== EMPTY_TENANT) {
    return reply(404, { detail: "not_found" });
  }
  if (request.method === "GET") {
    return reply(200, { workspaces: tenant === TENANT ? [WORKSPACE] : [] });
  }
  const name = JSON.parse(body || "{}").name;
  if (name === "rejected-by-api") return reply(422, { detail: "invalid_name" });
  return reply(201, { ...WORKSPACE, name });
});
api.listen(0, "127.0.0.1");
await once(api, "listening");

const port = await freePort();
const base = `http://127.0.0.1:${port}`;
const server = spawn(
  process.execPath,
  [
    join(
      import.meta.dirname,
      "..",
      "node_modules",
      "next",
      "dist",
      "bin",
      "next",
    ),
    "start",
    "-p",
    String(port),
    "-H",
    "127.0.0.1",
  ],
  {
    cwd: join(import.meta.dirname, ".."),
    env: {
      PATH: process.env.PATH,
      NODE_ENV: "production",
      API_BASE_URL: `http://127.0.0.1:${api.address().port}`,
      APP_ORIGIN,
      OIDC_ISSUER: "https://idp.invalid/",
      OIDC_CLIENT_ID: "client",
      OIDC_CLIENT_SECRET: "not-a-real-secret",
      OIDC_AUDIENCE: "https://api.test",
      SESSION_SECRET: SECRET,
    },
    stdio: ["ignore", "pipe", "pipe"],
  },
);
let serverLog = "";
server.stdout.on("data", (d) => (serverLog += d));
server.stderr.on("data", (d) => (serverLog += d));

try {
  await waitForServer(base);
  const now = Math.floor(Date.now() / 1000);
  const session = (token, createdAt) =>
    sealData(
      { v: 1, at: token, exp: now + 600, iat: createdAt },
      { password: { 1: SECRET }, ttl: 600 },
    );
  const get = (path, cookie, init = {}) =>
    fetch(base + path, {
      redirect: "manual",
      ...init,
      headers: {
        ...(cookie ? { Cookie: `__Host-session=${cookie}` } : {}),
        ...init.headers,
      },
    });
  const toLogin = (r) =>
    [307, 303].includes(r.status) &&
    r.headers.get("location")?.endsWith("/auth/login?returnTo=%2Faccount");

  console.log("check-auth-runtime (next start, production build):");
  const home = await get("/");
  check(
    "home renders without a session",
    home.status === 200 && (await home.text()).includes('href="/account"'),
  );

  const anonymous = await get("/account");
  check(
    "/account without a session redirects to login",
    toLogin(anonymous),
    `${anonymous.status} ${anonymous.headers.get("location")}`,
  );
  check(
    "/account is no-store",
    anonymous.headers.get("cache-control") === "no-store",
    String(anonymous.headers.get("cache-control")),
  );
  check(
    // Not no-referrer: browsers then send `Origin: null` on the page's own logout
    // POST, which the Origin check refuses (found in the live A5 test).
    "/account sends Referrer-Policy same-origin",
    anonymous.headers.get("referrer-policy") === "same-origin",
    String(anonymous.headers.get("referrer-policy")),
  );
  check(
    "the page redirect sets no cookie",
    anonymous.headers.getSetCookie().length === 0,
  );

  const forged = await get("/account", "Fe26.2**forged**cookie");
  check("/account with a forged cookie redirects to login", toLogin(forged));

  const good = await get("/account", await session("good-token", now - 120));
  const goodBody = await good.text();
  check(
    "/account with a valid session shows the API's data",
    good.status === 200 &&
      goodBody.includes("u-1") &&
      goodBody.includes(TENANT),
  );
  check(
    "/account links each membership to its workspaces",
    goodBody.includes(`href="/tenants/${TENANT}/workspaces"`),
  );
  // The signed-in frame (P2-B): Workspaces and Account are always in the rail,
  // the current place is marked, and there is no placeholder control.
  const accountRail =
    /<nav aria-label="Primary" class="rail-nav">[\s\S]*?<\/nav>/.exec(
      goodBody,
    )?.[0] ?? "";
  check(
    "account: the rail offers Workspaces (to the user's tenant) and Account, Account is current",
    accountRail.includes(`href="/tenants/${TENANT}/workspaces"`) &&
      /<a href="\/account" aria-current="page">/.test(accountRail),
    accountRail.slice(0, 200),
  );
  check(
    "account: with one tenant the rail also offers that tenant's workspaces as shortcuts",
    accountRail.includes(
      `href="/tenants/${TENANT}/workspaces/${WORKSPACE.id}"`,
    ),
  );
  const membershipRow =
    /<ul class="workspaces memberships">[\s\S]*?<\/ul>/.exec(goodBody)?.[0] ??
    "";
  check(
    "account: a membership is a full-row link (same row as the workspace directory) with its role badge; the user and tenant IDs are one click away in a Technical details disclosure",
    new RegExp(
      `<li><a class="name" href="/tenants/${TENANT}/workspaces">Workspaces<span class="visually-hidden"> of tenant ${TENANT}, your role: owner</span></a><span class="meta"><span class="badge">owner</span></span></li>`,
    ).test(membershipRow.replace(/<!-- -->/g, "")) &&
      !goodBody.includes("Open workspaces") &&
      goodBody
        .replace(/<!-- -->/g, "")
        .includes(
          `<details class="id-details"><summary>Technical details</summary><dl><dt>User ID</dt><dd><code>u-1</code></dd><dt>Tenant ID</dt><dd><code>${TENANT}</code></dd></dl></details>`,
        ) &&
      !/User ID: /.test(goodBody),
    membershipRow.slice(0, 300),
  );
  check(
    "account: no dead navigation (every rail link is a real href)",
    [...accountRail.matchAll(/<a [^>]*>/g)].every((tag) =>
      /href="\/[^"]*"/.test(tag[0]),
    ),
  );
  check(
    "the page never renders the access token",
    !goodBody.includes("good-token"),
  );
  const form = goodBody.match(/<form[^>]*>/)?.[0] ?? "";
  check(
    "the page offers a POST logout form",
    /method="post"/.test(form) && /action="\/auth\/logout"/.test(form),
    form,
  );

  const rejected = await get(
    "/account",
    await session("revoked-token", now - 120),
  );
  check(
    "API 401 on an older session redirects to login (which clears the cookie)",
    toLogin(rejected),
    `${rejected.status}`,
  );
  check(
    "that redirect itself sets no cookie",
    rejected.headers.getSetCookie().length === 0,
  );

  const fresh = await get("/account", await session("revoked-token", now));
  const freshBody = await fresh.text();
  check(
    "API 401 on a brand-new session shows an error instead of looping",
    fresh.status === 200 && freshBody.includes("rejected this sign-in"),
  );

  const login = await get(
    "/auth/login?returnTo=/account",
    await session("good-token", now),
  );
  check("login without a reachable IdP is 503", login.status === 503);
  check("login 503 sets no cookies", login.headers.getSetCookie().length === 0);
  check(
    "/auth/* is no-store",
    login.headers.get("cache-control") === "no-store",
  );
  check(
    "/auth/* sends no-referrer",
    login.headers.get("referrer-policy") === "no-referrer",
  );

  const callback = await get("/auth/callback?code=x&state=y");
  const cleared = callback.headers.getSetCookie();
  check("callback without a transaction is 400", callback.status === 400);
  check(
    "callback clears both cookies with __Host- attributes",
    ["__Host-session", "__Host-oidc"].every((name) =>
      cleared.some(
        (c) =>
          c.startsWith(`${name}=;`) &&
          /Max-Age=0/i.test(c) &&
          /Secure/i.test(c) &&
          /HttpOnly/i.test(c) &&
          /Path=\//.test(c) &&
          !/Domain=/i.test(c),
      ),
    ),
    JSON.stringify(cleared),
  );

  const logoutGet = await get("/auth/logout");
  check("logout by GET is not allowed (405)", logoutGet.status === 405);
  const crossSite = await get("/auth/logout", null, {
    method: "POST",
    headers: { Origin: "https://evil.test" },
  });
  check(
    "cross-origin logout POST is refused (403)",
    crossSite.status === 403 && crossSite.headers.getSetCookie().length === 0,
  );
  const noOrigin = await get("/auth/logout", null, { method: "POST" });
  check("logout POST without Origin is refused (403)", noOrigin.status === 403);
  const nullOrigin = await get("/auth/logout", null, {
    method: "POST",
    headers: { Origin: "null" },
  });
  check(
    "logout POST with Origin: null is refused (403)",
    nullOrigin.status === 403,
  );
  const logout = await get("/auth/logout", await session("good-token", now), {
    method: "POST",
    headers: { Origin: APP_ORIGIN },
  });
  check(
    "same-origin logout clears the session and redirects",
    logout.status === 303 &&
      logout.headers
        .getSetCookie()
        .some(
          (c) =>
            c.startsWith("__Host-session=;") &&
            /Max-Age=0/i.test(c) &&
            /Secure/i.test(c),
        ),
    `${logout.status} ${JSON.stringify(logout.headers.getSetCookie())}`,
  );

  // --- A6: tenant workspaces ---
  const list = `/tenants/${TENANT}/workspaces`;
  const member = await session("good-token", now - 120);
  const toLoginFrom = (r, path) =>
    [307, 303].includes(r.status) &&
    r.headers
      .get("location")
      ?.endsWith(`/auth/login?returnTo=${encodeURIComponent(path)}`);

  const noSession = await get(list);
  check(
    "workspaces without a session redirect to login for that page",
    toLoginFrom(noSession, list),
    `${noSession.status} ${noSession.headers.get("location")}`,
  );
  check(
    "workspaces are no-store with Referrer-Policy same-origin",
    noSession.headers.get("cache-control") === "no-store" &&
      noSession.headers.get("referrer-policy") === "same-origin",
  );

  const listed = await get(list, member);
  const listedBody = await listed.text();
  check("member sees the workspace list (200)", listed.status === 200);
  check(
    "workspace names are escaped, never rendered as HTML",
    listedBody.includes("&lt;b&gt;Contracts&lt;/b&gt; &amp; co") &&
      !listedBody.includes("<b>Contracts</b>"),
  );
  check(
    "workspaces: the tenant ID is in a Details disclosure, not loose text",
    listedBody
      .replace(/<!-- -->/g, "")
      .includes(
        `<details class="id-details"><summary>Details</summary><dl><dt>Tenant ID</dt><dd><code>${TENANT}</code></dd></dl></details>`,
      ) && !/Tenant ID <code>/.test(listedBody),
  );
  check(
    "creation time is a machine-readable <time>",
    listedBody.includes(`dateTime="${WORKSPACE.created_at}"`),
  );
  const listedRail =
    /<nav aria-label="Primary" class="rail-nav">[\s\S]*?<\/nav>/.exec(
      listedBody,
    )?.[0] ?? "";
  check(
    "tenant pages: the rail lists the tenant's workspaces as real links, names escaped, Workspaces current",
    listedRail.includes(`href="${list}/${WORKSPACE.id}"`) &&
      listedRail.includes("&lt;b&gt;Contracts&lt;/b&gt; &amp; co") &&
      !listedRail.includes("<b>Contracts</b>") &&
      new RegExp(`<a href="${list}" aria-current="page">`).test(listedRail),
    listedRail.slice(0, 240),
  );
  check(
    "workspace index: breadcrumb, one New workspace disclosure that contains the create form",
    /<nav aria-label="Breadcrumb" class="breadcrumbs">[\s\S]*?<span aria-current="page">Workspaces<\/span>/.test(
      listedBody,
    ) &&
      /<details class="popover new-workspace">\s*<summary class="button-primary">[\s\S]*?New workspace<\/summary>[\s\S]*?<form[^>]*action="[^"]*\/create"/.test(
        listedBody,
      ),
  );
  const createForm = listedBody.match(/<form[^>]*>/)?.[0] ?? "";
  check(
    "create form posts to the create route",
    /method="post"/.test(createForm) &&
      createForm.includes(`action="${list}/create"`),
    createForm,
  );
  check(
    "name input is labelled, required, bounded and described",
    /<label[^>]*for="workspace-name"/.test(listedBody) &&
      /<input[^>]*id="workspace-name"[^>]*>/.test(listedBody) &&
      /<input[^>]*required[^>]*>/.test(listedBody) &&
      /<input[^>]*maxLength="200"[^>]*>/i.test(listedBody) &&
      /aria-describedby="workspace-name-hint"/.test(listedBody),
  );
  check(
    "the page never renders the access token",
    !listedBody.includes("good-token"),
  );

  const empty = await (
    await get(`/tenants/${EMPTY_TENANT}/workspaces`, member)
  ).text();
  check(
    "empty tenant shows the empty state",
    empty.includes("No workspaces yet."),
  );

  const down = await (
    await get(`/tenants/${DOWN_TENANT}/workspaces`, member)
  ).text();
  check(
    "API outage shows an error state with a retry link",
    /role="alert"[^>]*>Workspaces are unavailable right now/.test(down),
  );

  const unknown = await get(
    "/tenants/01a0e9bd-65a9-700c-a38b-c2210bcb6399/workspaces",
    member,
  );
  const unknownBody = await unknown.text();
  check(
    "non-member or unknown tenant: 404 with the uniform page",
    unknown.status === 404 && unknownBody.includes("Tenant not found"),
    String(unknown.status),
  );
  const callsBefore = apiCalls.length;
  const malformed = await get("/tenants/not-a-uuid/workspaces", member);
  const malformedBody = await malformed.text();
  check(
    "malformed tenant ID: same 404 page, and the API is not called",
    malformed.status === 404 &&
      malformedBody.includes("Tenant not found") &&
      apiCalls.length === callsBefore,
  );

  check(
    "rejected token on an older session redirects to login",
    toLoginFrom(
      await get(list, await session("revoked-token", now - 120)),
      list,
    ),
  );
  const freshList = await (
    await get(list, await session("revoked-token", now))
  ).text();
  check(
    "rejected token on a fresh session shows an error, no loop",
    freshList.includes("rejected this sign-in"),
  );

  const flash = await (await get(`${list}?created=1`, member)).text();
  check(
    "success message is announced (role=status)",
    /role="status"[^>]*>Workspace created\./.test(flash),
  );
  const nameErr = await (
    await get(`${list}?error=invalid_name`, member)
  ).text();
  check(
    "invalid-name error is announced and tied to the field",
    /id="workspace-name-error" role="alert"/.test(nameErr) &&
      /aria-invalid="true"/.test(nameErr) &&
      /aria-describedby="workspace-name-hint workspace-name-error"/.test(
        nameErr,
      ),
  );
  const injected = await (
    await get(
      `${list}?error=%3Cscript%3Ealert(1)%3C%2Fscript%3E&created=%3Cb%3E`,
      member,
    )
  ).text();
  check(
    "unknown flash codes render nothing from the URL",
    // Next's own route announcer uses role="alert", and its router data carries
    // the percent-encoded query string; check for rendered content instead.
    !injected.includes("<script>alert(1)") &&
      !injected.includes("&lt;script") &&
      !injected.includes("Workspace created.") &&
      !/could not be created|Enter a name of|rejected this sign-in/.test(
        injected,
      ),
    JSON.stringify(
      ["alert(1)", "&lt;script", "Workspace created.", "Enter a name of"].map(
        (needle) => {
          const at = injected.indexOf(needle);
          return at < 0 ? null : injected.slice(Math.max(0, at - 80), at + 40);
        },
      ),
    ),
  );

  const create = (headers, body, cookie = member, tenant = TENANT) =>
    get(`/tenants/${tenant}/workspaces/create`, cookie, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        ...headers,
      },
      body,
    });
  const createCalls = () => apiCalls.filter((c) => c.method === "POST").length;

  const postsBefore = createCalls();
  for (const [label, headers] of [
    ["no Origin", {}],
    ["foreign Origin", { Origin: "https://evil.test" }],
    ["Origin: null", { Origin: "null" }],
  ]) {
    const refused = await create(headers, "name=x");
    check(`create with ${label} is refused (403)`, refused.status === 403);
  }
  check("refused creates never reach the API", createCalls() === postsBefore);

  const same = { Origin: APP_ORIGIN };
  const created = await create(same, "name=Quarterly+reports");
  check(
    "create: 303 to the list with the success code",
    created.status === 303 &&
      created.headers.get("location") === `${APP_ORIGIN}${list}?created=1`,
    `${created.status} ${created.headers.get("location")}`,
  );
  check(
    "create sends the name to the API as JSON",
    apiCalls.at(-1)?.body === JSON.stringify({ name: "Quarterly reports" }),
  );
  const blank = await create(same, "name=%20%20");
  check(
    "blank name: fixed error code, no API call",
    blank.headers.get("location") ===
      `${APP_ORIGIN}${list}?error=invalid_name` &&
      createCalls() === postsBefore + 1,
  );
  const apiRejects = await create(same, "name=rejected-by-api");
  check(
    "API 422: fixed error code",
    apiRejects.headers.get("location") ===
      `${APP_ORIGIN}${list}?error=invalid_name`,
  );
  const foreign = await create(
    same,
    "name=x",
    member,
    "01a0e9bd-65a9-700c-a38b-c2210bcb6399",
  );
  check(
    "create in a non-member tenant: back to the list, which is 404",
    foreign.headers.get("location") ===
      `${APP_ORIGIN}/tenants/01a0e9bd-65a9-700c-a38b-c2210bcb6399/workspaces`,
  );
  const badTenant = await create(same, "name=x", member, "not-a-uuid");
  check("create with a malformed tenant ID is 404", badTenant.status === 404);
  const anonymousCreate = await create(same, "name=x", null);
  check(
    "create without a session goes to login",
    toLoginFrom(anonymousCreate, list),
  );
  const createGet = await get(`${list}/create`, member);
  check("create by GET is not allowed (405)", createGet.status === 405);

  // --- B1: workspace documents ---
  const page = `${list}/${WORKSPACE.id}`;
  const uploadPath = `${page}/documents/upload`;
  const seeded = documents.at(-1);
  const downloadPath = (id) => `${page}/documents/${id}/download`;

  const docPage = await get(page, member);
  const docBody = await docPage.text();
  check(
    "workspace page renders for a member (200, no-store)",
    docPage.status === 200 &&
      docPage.headers.get("cache-control") === "no-store",
    String(docPage.status),
  );
  check(
    "workspace list links to the workspace page",
    listedBody.includes(`href="${page}"`),
  );
  check(
    "workspace and document names are escaped",
    docBody.includes("&lt;b&gt;Contracts&lt;/b&gt; &amp; co") &&
      docBody.includes("&lt;i&gt;Seeded&lt;/i&gt; report.pdf") &&
      !docBody.includes("<i>Seeded</i>"),
  );
  check(
    "document table: caption, column headers, row header, sha256 and <time>",
    /<table class="documents">/.test(docBody) &&
      /<caption/.test(docBody) &&
      /<th scope="col">Document<\/th>/.test(docBody) &&
      /<th scope="row"/.test(docBody) &&
      docBody.includes(`title="${sha256(SEEDED_PDF)}"`) &&
      docBody.includes(`dateTime="${seeded.created_at}"`),
  );
  check(
    "each document has a download link with its name as hidden context",
    docBody.includes(`href="${downloadPath(seeded.id)}"`) &&
      /Download<span class="visually-hidden"> (?:<!-- -->)?&lt;i&gt;Seeded/.test(
        docBody,
      ),
  );
  // One upload form, reached from the toolbar and from the list by plain links;
  // document names are links and the list says its status column is the scan.
  check(
    "documents: one upload form (#upload) on the page, always shown, with a drop zone and a toolbar link to it; the scan and an Open action are separate columns",
    (docBody.match(/<form[^>]*multipart/g) ?? []).length === 1 &&
      (docBody.match(/id="upload"/g) ?? []).length === 1 &&
      (docBody.match(/href="#upload"/g) ?? []).length === 1 &&
      /<div class="dropzone"><input[^>]*id="document-file"/.test(docBody) &&
      !docBody.includes("upload-cancel") &&
      /<th scope="col">Malware scan<\/th><th scope="col" class="open"><span class="visually-hidden">Open<\/span><\/th>/.test(
        docBody,
      ) &&
      /<td class="open"><a href="[^"]*\/documents\/[^"]*">Open<span class="visually-hidden"> [^<]*<\/span><\/a><\/td>/.test(
        docBody.replace(/<!-- -->/g, ""),
      ) &&
      !docBody.includes("Open to process or review") &&
      !docBody.includes("Next step") &&
      !/Processed|Needs review|Queued/.test(
        docBody.slice(docBody.indexOf('<table class="documents">')),
      ),
  );
  check(
    "documents: the uploaded date is short, with the full UTC time on the element (title and dateTime)",
    /<td class="uploaded"><time datetime="[^"]+" title="\d{1,2} [A-Za-z]+ \d{4}, \d{2}:\d{2} UTC">\d{1,2} [A-Za-z]+ \d{4}<\/time><\/td>/i.test(
      docBody.replace(/<!-- -->/g, ""),
    ) && !/<td class="uploaded"><time[^>]*>[^<]*UTC<\/time>/.test(docBody),
  );
  const uploadForm = docBody.match(/<form[^>]*>/)?.[0] ?? "";
  check(
    "upload form: multipart POST to the upload route",
    /method="post"/.test(uploadForm) &&
      /enctype="multipart\/form-data"/i.test(uploadForm) &&
      uploadForm.includes(`action="${uploadPath}"`),
    uploadForm,
  );
  check(
    "file input is labelled, required, PDF-only and described",
    /<label[^>]*for="document-file"/.test(docBody) &&
      /<input[^>]*type="file"[^>]*>/.test(docBody) &&
      /<input[^>]*id="document-file"[^>]*required[^>]*>/.test(docBody) &&
      /accept="application\/pdf,.pdf"/.test(docBody) &&
      /aria-describedby="document-file-hint"/.test(docBody),
  );
  const hint =
    /<p id="document-file-hint"[^>]*>([\s\S]*?)<\/p>/.exec(docBody)?.[1] ?? "";
  check(
    "the file hint says uploads are scanned before storage",
    hint.includes("scanned for malware before it is stored") &&
      hint.includes("Encrypted PDFs are not accepted.") &&
      !hint.includes("not opened or scanned"),
    hint,
  );
  check(
    "the page never renders the access token",
    !docBody.includes("good-token"),
  );

  for (const [label, path] of [
    ["unknown workspace", `${list}/01a0e9d1-6f65-7ac6-b4b3-84557cd2b199`],
    [
      "workspace of a non-member tenant",
      `/tenants/01a0e9bd-65a9-700c-a38b-c2210bcb6399/workspaces/${WORKSPACE.id}`,
    ],
  ]) {
    const response = await get(path, member);
    check(
      `${label}: 404 with the uniform workspace page`,
      response.status === 404 &&
        (await response.text()).includes("Workspace not found"),
      String(response.status),
    );
  }
  const callsBeforeMalformed = apiCalls.length;
  const malformedWorkspace = await get(`${list}/not-a-uuid`, member);
  await malformedWorkspace.text();
  // The signed-in frame lists the tenant's workspaces (the navigation's own
  // read); the malformed ID itself must never reach a workspace endpoint.
  check(
    "malformed workspace ID: 404 and no workspace or document endpoint is called",
    malformedWorkspace.status === 404 &&
      apiCalls
        .slice(callsBeforeMalformed)
        .every(
          (call) =>
            call.method === "GET" &&
            call.url === `/v1/tenants/${TENANT}/workspaces`,
        ),
  );
  check(
    "workspace page without a session redirects to login for it",
    toLoginFrom(await get(page), page),
  );

  const flashUploaded = await (await get(`${page}?uploaded=1`, member)).text();
  check(
    "upload success is announced (role=status)",
    /role="status"[^>]*>Document uploaded\./.test(flashUploaded),
  );
  for (const [code, text] of [
    ["too_large", "larger than 20 MiB"],
    ["not_pdf", "Only PDF files"],
    ["invalid_name", "file name cannot be used"],
    ["missing_file", "Choose a PDF file"],
    ["unavailable", "could not be uploaded right now"],
    ["malware_detected", "malware scan found a threat"],
    ["scan_incomplete", "could not be fully scanned"],
    ["encrypted_pdf", "encrypted PDFs cannot be uploaded"],
    ["scan_unavailable", "Malware scanning is unavailable right now"],
    ["upload_rejected", "The file was rejected and not stored"],
  ]) {
    const html = await (await get(`${page}?error=${code}`, member)).text();
    const form = /<form[^>]*multipart[\s\S]*?<\/form>/.exec(html)?.[0] ?? "";
    const input = /<input[^>]*id="document-file"[^>]*>/.exec(form)?.[0] ?? "";
    check(
      `upload error ${code}: an alert next to the file field, tied to it`,
      new RegExp(
        `<p id="document-file-error" role="alert" class="error">[^<]*${text}`,
      ).test(form) &&
        /aria-invalid="true"/.test(input) &&
        /aria-describedby="document-file-hint document-file-error"/.test(input),
      input,
    );
    // Rendered markup only: the serialized client props in <script> carry
    // the message too.
    const markup = html.replace(/<script\b[\s\S]*?<\/script>/g, "");
    check(
      `upload error ${code}: shown once, not repeated at the top`,
      markup.split(text).length === 2 &&
        (markup.match(/role="alert"/g) ?? []).length === 1,
      String(markup.split(text).length - 1),
    );
  }
  const plainForm =
    /<form[^>]*multipart[\s\S]*?<\/form>/.exec(docBody)?.[0] ?? "";
  check(
    // aria-invalid="false" must be explicit: with only `required`, browsers
    // expose the empty field as invalid before any interaction (VoiceOver:
    // "required, invalid data"; found in the B1 live check).
    "without an error code the file field is explicitly valid, required, and not tied to an error",
    !plainForm.includes("document-file-error") &&
      /<input[^>]*id="document-file"[^>]*aria-invalid="false"/.test(
        plainForm,
      ) &&
      /<input[^>]*id="document-file"[^>]*required/.test(plainForm) &&
      /aria-describedby="document-file-hint"/.test(plainForm),
    /<input[^>]*id="document-file"[^>]*>/.exec(plainForm)?.[0] ?? "",
  );
  const pageRejected = await (
    await get(`${page}?error=rejected`, member)
  ).text();
  check(
    "page-level error (rejected) stays at the top, outside the form",
    /<\/h1><p role="alert">The API rejected this sign-in/.test(pageRejected) &&
      !(
        /<form[^>]*multipart[\s\S]*?<\/form>/.exec(pageRejected)?.[0] ?? ""
      ).includes("rejected"),
  );
  const docInjected = await (
    await get(`${page}?error=%3Cscript%3E&uploaded=%3Cb%3E`, member)
  ).text();
  check(
    "unknown document flash codes render nothing from the URL",
    !docInjected.includes("&lt;script") &&
      !docInjected.includes("Document uploaded.") &&
      !/Only PDF files|could not be uploaded/.test(docInjected),
  );

  const multipart = (
    bytes,
    name = "Quarterly report.pdf",
    type = "application/pdf",
  ) => {
    const body = new FormData();
    body.append("file", new File([bytes], name, { type }));
    return body;
  };
  const upload = (
    body,
    headers = { Origin: APP_ORIGIN },
    cookie = member,
    path = uploadPath,
  ) => get(path, cookie, { method: "POST", headers, body });
  const documentPosts = () =>
    apiCalls.filter((c) => c.method === "POST" && c.url.endsWith("/documents"));

  // 12 MiB: above the proxy's 10 MB request-body buffer, below the 20 MiB limit.
  const big = Buffer.alloc(12 * 1024 * 1024, 0x20);
  big.write("%PDF-1.7\n");
  big.write("%%EOF\n", big.byteLength - 6);
  const uploaded = await upload(multipart(big));
  const received = documentPosts().at(-1);
  check(
    "upload of 12 MiB: 303 back to the page with the success code",
    uploaded.status === 303 &&
      uploaded.headers.get("location") === `${APP_ORIGIN}${page}?uploaded=1`,
    `${uploaded.status} ${uploaded.headers.get("location")}`,
  );
  check(
    "upload of 12 MiB reaches the API byte-identical (not truncated at 10 MB)",
    received?.size === big.byteLength && received?.sha256 === sha256(big),
    `${received?.size} bytes`,
  );
  check(
    "upload forwards the bearer token, type and encoded name",
    received?.headers.authorization === "Bearer good-token" &&
      received?.headers["content-type"] === "application/pdf" &&
      received?.headers["x-document-name"] === "Quarterly%20report.pdf",
  );
  check(
    "upload responses are no-store",
    uploaded.headers.get("cache-control") === "no-store",
  );
  const listedAfter = await (await get(page, member)).text();
  check(
    "the uploaded document is listed with its SHA-256",
    listedAfter.includes("Quarterly report.pdf") &&
      listedAfter.includes(`title="${sha256(big)}"`),
  );

  const newest = documents[0];
  const download = await get(downloadPath(newest.id), member);
  const downloaded = Buffer.from(await download.arrayBuffer());
  check(
    "download returns the exact uploaded bytes",
    download.status === 200 &&
      downloaded.byteLength === big.byteLength &&
      sha256(downloaded) === sha256(big),
    `${download.status} ${downloaded.byteLength}`,
  );
  check(
    "download is an attachment with nosniff, sandbox CSP and no-store",
    download.headers.get("content-type") === "application/pdf" &&
      (download.headers.get("content-disposition") ?? "").startsWith(
        "attachment;",
      ) &&
      download.headers.get("x-content-type-options") === "nosniff" &&
      download.headers.get("content-security-policy") ===
        "sandbox; default-src 'none'" &&
      download.headers.get("cache-control") === "no-store",
    JSON.stringify(Object.fromEntries(download.headers)),
  );
  const unknownDoc = await get(
    downloadPath("01a0f000-0000-7000-8000-0000000000ff"),
    member,
  );
  check("download of an unknown document is 404", unknownDoc.status === 404);
  const docCalls = apiCalls.length;
  const badDoc = await get(downloadPath("not-a-uuid"), member);
  check(
    "download with a malformed ID is 404 without an API call",
    badDoc.status === 404 && apiCalls.length === docCalls,
  );
  check(
    "download without a session redirects to login",
    toLoginFrom(await get(downloadPath(seeded.id)), page),
  );
  const foreignDoc = await get(
    `/tenants/01a0e9bd-65a9-700c-a38b-c2210bcb6399/workspaces/${WORKSPACE.id}/documents/${seeded.id}/download`,
    member,
  );
  check("download from a non-member tenant is 404", foreignDoc.status === 404);

  const postsBeforeRefusals = documentPosts().length;
  for (const [label, headers] of [
    ["no Origin", {}],
    ["foreign Origin", { Origin: "https://evil.test" }],
    ["Origin: null", { Origin: "null" }],
  ]) {
    const refused = await upload(multipart(SEEDED_PDF), headers);
    check(`upload with ${label} is refused (403)`, refused.status === 403);
  }
  const overLimit = Buffer.alloc(DOCUMENT_MAX_BYTES + 1, 0x20);
  overLimit.write("%PDF-1.7\n");
  const tooLarge = await upload(multipart(overLimit));
  check(
    "upload over 20 MiB: fixed too_large code",
    tooLarge.headers.get("location") === `${APP_ORIGIN}${page}?error=too_large`,
    String(tooLarge.headers.get("location")),
  );
  const noFile = await upload(new FormData());
  check(
    "upload without a file: fixed missing_file code",
    noFile.headers.get("location") ===
      `${APP_ORIGIN}${page}?error=missing_file`,
  );
  check(
    "refused, oversized and empty uploads never reach the API",
    documentPosts().length === postsBeforeRefusals,
  );
  const notPdf = await upload(multipart(Buffer.from("<html>"), "a.pdf"));
  check(
    "API 415: fixed not_pdf code",
    notPdf.headers.get("location") === `${APP_ORIGIN}${page}?error=not_pdf`,
  );
  const callsBeforeForeign = apiCalls.length;
  const foreignUpload = await upload(
    multipart(SEEDED_PDF),
    { Origin: APP_ORIGIN },
    member,
    `/tenants/01a0e9bd-65a9-700c-a38b-c2210bcb6399/workspaces/${WORKSPACE.id}/documents/upload`,
  );
  check(
    "upload to a non-member tenant: back to the page, which is 404",
    foreignUpload.headers.get("location") ===
      `${APP_ORIGIN}/tenants/01a0e9bd-65a9-700c-a38b-c2210bcb6399/workspaces/${WORKSPACE.id}`,
  );
  const foreignCalls = apiCalls.slice(callsBeforeForeign);
  check(
    "non-member upload (D-058): only the workspace check reaches the API, no upload is sent",
    foreignCalls.length === 1 &&
      foreignCalls[0].method === "GET" &&
      foreignCalls[0].url ===
        `/v1/tenants/01a0e9bd-65a9-700c-a38b-c2210bcb6399/workspaces/${WORKSPACE.id}`,
    JSON.stringify(foreignCalls.map((c) => `${c.method} ${c.url}`)),
  );
  const callsBeforeMember = apiCalls.length;
  await upload(multipart(SEEDED_PDF));
  const memberCalls = apiCalls
    .slice(callsBeforeMember)
    .map((c) => `${c.method} ${c.url}`);
  check(
    "member upload: workspace check first, then the upload",
    memberCalls.length === 2 &&
      memberCalls[0] ===
        `GET /v1/tenants/${TENANT}/workspaces/${WORKSPACE.id}` &&
      memberCalls[1] ===
        `POST /v1/tenants/${TENANT}/workspaces/${WORKSPACE.id}/documents`,
    JSON.stringify(memberCalls),
  );
  check(
    "upload without a session goes to login",
    toLoginFrom(
      await upload(multipart(SEEDED_PDF), { Origin: APP_ORIGIN }, null),
      page,
    ),
  );
  const uploadGet = await get(uploadPath, member);
  check("upload by GET is not allowed (405)", uploadGet.status === 405);

  // --- malware scanning (PR B): outcomes and scan status from the API ---
  const scanOutcome = async (marker) => {
    const bytes = Buffer.concat([SEEDED_PDF, Buffer.from(marker)]);
    return (await upload(multipart(bytes))).headers.get("location");
  };
  check(
    "API 422 malware_detected: its own fixed code, not invalid_name",
    (await scanOutcome(MALWARE_MARKER)) ===
      `${APP_ORIGIN}${page}?error=malware_detected`,
  );
  check(
    "API 503 scan_unavailable: its own fixed code, not unavailable",
    (await scanOutcome(SCANNER_DOWN_MARKER)) ===
      `${APP_ORIGIN}${page}?error=scan_unavailable`,
  );
  const cleanRows = await (await get(page, member)).text();
  check(
    "document table has a Malware scan column; clean versions say Clean and link to download",
    /<th scope="col">Malware scan<\/th>/.test(cleanRows) &&
      /<td><span class="badge badge--success">Clean<\/span><\/td>/.test(
        cleanRows,
      ) &&
      cleanRows.includes(`href="${downloadPath(seeded.id)}"`),
  );
  const scanPage = `${list}/${SCAN_WORKSPACE.id}`;
  const scanHtml = await (await get(scanPage, member)).text();
  check(
    "not scanned and blocked versions are listed with their status",
    /<td><span class="badge badge--attention">Not scanned<\/span><\/td>/.test(
      scanHtml,
    ) &&
      /<td><span class="badge badge--failed">Blocked<\/span><\/td>/.test(
        scanHtml,
      ),
  );
  check(
    "no download link for versions that are not clean",
    !scanHtml.includes("/download") &&
      // Rendered markup only: Next's serialized payload repeats the text.
      (
        scanHtml
          .replace(/<script\b[\s\S]*?<\/script>/g, "")
          .match(/Not available/g) ?? []
      ).length === 2,
    String((scanHtml.match(/\/download/g) ?? []).length),
  );
  for (const doc of scanDocuments) {
    const direct = await get(
      `${scanPage}/documents/${doc.id}/download`,
      member,
    );
    check(
      `download of a ${doc.scan} version (API 409): back to the page with a fixed code`,
      direct.status === 303 &&
        direct.headers.get("location") === `${scanPage}?error=${doc.scan}`,
      `${direct.status} ${direct.headers.get("location")}`,
    );
  }
  for (const [code, text] of [
    ["not_scanned", "has not been scanned for malware"],
    ["blocked", "is blocked by the malware scan"],
  ]) {
    const html = await (await get(`${scanPage}?error=${code}`, member)).text();
    check(
      `download refusal ${code} is announced at the top (role=alert)`,
      new RegExp(`</h1><p role="alert">[^<]*${text}`).test(html),
    );
  }

  // --- DI-1: document page (processing states, evidence, process route) ---
  {
    const procPath = `${list}/${PROC_WORKSPACE.id}`;
    const docPath = (state) =>
      `${procPath}/documents/${procDocuments.find((d) => d.state === state).id}`;
    const render = async (path) => {
      const response = await get(path, member);
      return { status: response.status, body: await response.text() };
    };
    const pages = {};
    for (const state of procDocuments.map((d) => d.state)) {
      pages[state] = await render(docPath(state));
    }
    const text = (html) =>
      html
        .replace(/<!-- -->/g, "")
        .replace(/<[^>]+>/g, " ")
        .replace(/\s+/g, " ");
    check(
      "every processing state renders the document page (200)",
      Object.values(pages).every((p) => p.status === 200),
      JSON.stringify(
        Object.fromEntries(
          Object.entries(pages).map(([k, v]) => [k, v.status]),
        ),
      ),
    );
    check(
      "the workspace list links each document name to its page",
      (await render(procPath)).body.includes(`href="${docPath("never")}"`),
    );
    const processForm = (body) =>
      /<form(?=[^>]*method="post")(?=[^>]*action="[^"]*\/process")[^>]*>/.test(
        body,
      ) && body.includes(">Process document</button>");
    check(
      "never processed: the state in words and the Process document form",
      text(pages.never.body).includes("Processing Not processed yet.") &&
        processForm(pages.never.body) &&
        pages.never.body.includes(`action="${docPath("never")}/process"`),
    );
    check(
      "never processed: one Process form in the page with its one primary button, and no second Process button in the toolbar",
      (pages.never.body.match(/<form[^>]*\/process"/g) ?? []).length === 1 &&
        pages.never.body.includes('id="process-form"') &&
        !pages.never.body.includes('form="process-form"') &&
        (pages.never.body.match(/>Process document</g) ?? []).length === 1 &&
        text(pages.never.body).includes("Next: process this document"),
    );
    check(
      "loading state: the miniature page appears beside the status only for queued (waiting) and running (being read), is decorative, and is gone for finished and failed runs",
      /<span role="status">[^<]*<\/span><svg class="doc-scan" data-state="queued"[^>]*aria-hidden="true"/.test(
        pages.queued.body,
      ) &&
        /<span role="status">[^<]*<\/span><svg class="doc-scan" data-state="running"[^>]*aria-hidden="true"/.test(
          pages.running.body,
        ) &&
        ["never", "completed", "needs_review", "failed"].every(
          (state) => !pages[state].body.includes('class="doc-scan"'),
        ),
    );
    check(
      "failed run: the reason, no Process button, and the supported recovery (upload a corrected copy); no invented retry",
      pages.failed.body.includes(
        "This document can&#x27;t be processed again",
      ) &&
        /<a class="button-secondary" href="[^"]*#upload">Upload a corrected copy<\/a>/.test(
          pages.failed.body,
        ) &&
        !processForm(pages.failed.body) &&
        !/>\s*(Retry|Try again|Process again|Reprocess)\s*</i.test(
          pages.failed.body,
        ),
    );
    check(
      "the scan and processing facts are shown in words",
      text(pages.never.body).includes("Malware scan Clean"),
    );
    check(
      "queued and running: the state, a Check status link, no form and no record",
      text(pages.queued.body).includes("Queued — waiting to start.") &&
        text(pages.running.body).includes("Processing… Started") &&
        ["queued", "running"].every(
          (s) =>
            pages[s].body.includes(">Check status</a>") &&
            !processForm(pages[s].body) &&
            !pages[s].body.includes("Service agreement") &&
            // What to do (nothing) and why, in the page, not an invented stage.
            text(pages[s].body).includes("Nothing to do while this runs"),
        ) &&
        !text(pages.never.body).includes("Nothing to do while this runs") &&
        !text(pages.completed.body).includes("Nothing to do while this runs"),
    );
    const completed = pages.completed.body;
    check(
      "completed: seven quiet rows, a summary, found rows closed",
      text(completed).includes(
        "All required fields found · 6 of 7 fields found.",
      ) &&
        (completed.match(/class="field(?: [^"]*)?"/g) ?? []).length === 7 &&
        !/<details[^>]*open=""/.test(completed.split("About this result")[0]) &&
        !completed.includes("field--attention") &&
        !completed.includes(">Check status</a>"),
    );
    check(
      "found values are formatted; the quote shows the page and marks the raw value",
      text(completed).includes("1 March 2026") &&
        text(completed).includes("EUR 120,000.00") &&
        text(completed).includes("90 days") &&
        completed.includes("<figcaption>Page <!-- -->2</figcaption>") &&
        completed.includes("<mark>EUR 120,000.00</mark>"),
    );
    check(
      "values and quotes are escaped",
      completed.includes("&lt;b&gt;Law&lt;/b&gt; &amp; order") &&
        !completed.includes("<b>Law</b>"),
    );
    const review = pages.needs_review.body;
    check(
      "needs review: the state and a summary linking to each field that needs a look",
      text(review).includes("Processing Needs review — 2 fields.") &&
        review.includes("tone-attention") &&
        text(review).includes("Review needed — 2 of 7 fields:") &&
        review.includes('href="#field-customer-name"') &&
        review.includes('href="#field-effective-date"'),
    );
    // Each field is a link to its panel (#field-…); the panel holds the value,
    // the source text and the decisions.
    const pane = (html, id) =>
      new RegExp(
        `<section id="${id}" class="pane[^"]*" aria-labelledby="${id}-title">([\\s\\S]*?)</section>`,
      ).exec(html)?.[1] ?? "";
    check(
      "a missing required field says so in words, with no source text to show",
      /<li class="field field--attention field--unresolved"><a class="field-link" href="#field-customer-name"/.test(
        review,
      ) &&
        text(review).includes(
          "Customer Not found in the document Needs review",
        ) &&
        text(pane(review, "field-customer-name")).includes(
          "There is no source text for this field.",
        ) &&
        !pane(review, "field-customer-name").includes('class="evidence"'),
    );
    const conflict = pane(review, "field-effective-date");
    check(
      "conflict: every candidate side by side with its own page and quote, none preferred",
      text(conflict).includes("2 different values") &&
        text(review).includes("Compare 2") &&
        /<ol class="candidates">/.test(conflict) &&
        text(conflict).includes(
          "1 March 2026 Page 1 Effective Date: 1 March 2026",
        ) &&
        text(conflict).includes(
          "1 April 2026 Page 2 effective as of 1 April 2026",
        ),
      text(conflict),
    );
    check(
      "a value quoted twice lists both quotes",
      text(review).includes("Page 1 · 2 quotes") &&
        (review.match(/<mark>Northwind Analytics Ltd<\/mark>/g) ?? [])
          .length === 2,
    );
    check(
      "ambiguous and unparseable: the reason in words, the quote, no value invented",
      text(pane(review, "field-end-date")).includes(
        "Ambiguous: it can be read in more than one way",
      ) &&
        text(pane(review, "field-notice-period-days")).includes(
          "Found, but not a valid number of days",
        ) &&
        review.includes("<mark>&lt;script&gt;alert(1)&lt;/script&gt;</mark>") &&
        !review.includes("<script>alert(1)"),
    );
    check(
      "failed: the fixed reason, no record and no form",
      text(pages.failed.body).includes(
        "Processing failed: the document contains attached or embedded files, which are not processed.",
      ) &&
        pages.failed.body.includes("tone-failed") &&
        !pages.failed.body.includes("Service agreement") &&
        !processForm(pages.failed.body),
    );
    check(
      "a malformed or unavailable answer is shown as unavailable, never guessed",
      ["malformed", "unavailable"].every(
        (s) =>
          text(pages[s].body).includes(
            "The processing status is unavailable right now.",
          ) &&
          pages[s].body.includes(">Check status</a>") &&
          !pages[s].body.includes("Service agreement"),
      ),
    );
    check(
      "about this result: extractor and parser image (collapsed)",
      /<details class="about"><summary>About this result<\/summary>/.test(
        completed,
      ) &&
        text(completed).includes("Extractor rules-1") &&
        completed.includes(`title="sha256:${"ab".repeat(32)}"`),
    );
    const notClean = await render(
      `${list}/${SCAN_WORKSPACE.id}/documents/${scanDocuments[1].id}`,
    );
    check(
      "a document that did not pass the scan: no processing, no download",
      notClean.status === 200 &&
        text(notClean.body).includes("Malware scan Blocked") &&
        text(notClean.body).includes(
          "Only available for documents that passed the malware scan.",
        ) &&
        !processForm(notClean.body) &&
        !notClean.body.includes("/download"),
    );
    for (const [label, path] of [
      [
        "unknown document",
        `${procPath}/documents/01a0f000-0000-7000-8000-00000000dead`,
      ],
      ["malformed document ID", `${procPath}/documents/not-a-uuid`],
      [
        "another tenant",
        `/tenants/${EMPTY_TENANT}/workspaces/${PROC_WORKSPACE.id}/documents/${procDocuments[0].id}`,
      ],
    ]) {
      const missing = await render(path);
      check(
        `document page: ${label} is the uniform 404`,
        missing.status === 404,
        String(missing.status),
      );
    }
    const signedOut = await get(docPath("never"));
    check(
      "document page without a session goes to sign-in",
      [303, 307].includes(signedOut.status) &&
        signedOut.headers
          .get("location")
          ?.includes(`returnTo=${encodeURIComponent(docPath("never"))}`),
    );
    // PageNotice: the server renders the live region empty (screen readers
    // announce only changes, not text present at load) and the message only
    // inside <noscript>; the client fills the region after load.
    const notice = (role, text) =>
      `<p role="${role}" class="page-notice"></p><noscript><p>${text}</p></noscript>`;
    const startedFlash = await render(
      `${docPath("queued")}?processing=started`,
    );
    check(
      "a fixed flash message after starting (an empty live region, filled after load; the text in <noscript>); unknown codes are ignored",
      startedFlash.body.includes(notice("status", "Processing started.")) &&
        !(
          await render(`${docPath("never")}?processing=<b>x</b>`)
        ).body.includes("&lt;b&gt;x"),
    );
    check(
      "the document pages never render the access token",
      Object.values(pages).every((p) => !p.body.includes("good-token")),
    );

    check(
      "every state sits in one polite live region that survives a refresh (queued and running poll it)",
      Object.entries(pages).every(
        ([, page]) =>
          (page.body.match(/<span role="status">/g) ?? []).length === 1 &&
          /<dd class="tone-[a-z]+"><span role="status">/.test(page.body),
      ) &&
        ["queued", "running"].every((s) =>
          /<dd class="tone-working"><span role="status">/.test(pages[s].body),
        ),
    );
    const status = async (
      state,
      cookie = member,
      path = `${docPath(state)}/processing`,
    ) => {
      const response = await get(path, cookie, {
        headers: { Accept: "application/json" },
      });
      return {
        status: response.status,
        cache: response.headers.get("cache-control"),
        body: await response.text(),
      };
    };
    const statuses = {};
    for (const state of [
      "never",
      "queued",
      "running",
      "completed",
      "needs_review",
      "failed",
      "unavailable",
    ]) {
      statuses[state] = await status(state);
    }
    check(
      "status route: the run's state only, as JSON, never cached",
      statuses.queued.status === 200 &&
        statuses.queued.cache === "no-store" &&
        statuses.queued.body ===
          JSON.stringify({
            state: "ok",
            status: "queued",
            attempts: 0,
            startedAt: null,
          }) &&
        JSON.parse(statuses.never.body).state === "none" &&
        JSON.parse(statuses.failed.body).status === "failed" &&
        statuses.unavailable.status === 503,
      JSON.stringify(statuses.queued),
    );
    check(
      "status route: no result, quotes or document text ever",
      ["completed", "needs_review"].every(
        (s) =>
          Object.keys(JSON.parse(statuses[s].body)).sort().join() ===
            "attempts,startedAt,state,status" &&
          !/Northwind|Provider|Effective|quote/.test(statuses[s].body),
      ),
    );
    const notCleanStatus = await status(
      null,
      member,
      `${list}/${SCAN_WORKSPACE.id}/documents/${scanDocuments[1].id}/processing`,
    );
    const anonymousStatus = await status("queued", null);
    const badStatus = await status(
      null,
      member,
      `${procPath}/documents/nope/processing`,
    );
    check(
      "status route: not clean 409, signed out 401 (no redirect), malformed 404",
      notCleanStatus.status === 409 &&
        JSON.parse(notCleanStatus.body).state === "not_clean" &&
        anonymousStatus.status === 401 &&
        JSON.parse(anonymousStatus.body).state === "signed_out" &&
        badStatus.status === 404,
    );

    {
      // --- R1 review: controls, approval panel, routes, export
      const flat = (html) => html.replace(/<!-- -->/g, "");
      const formsIn = (html, suffix) =>
        [...html.matchAll(/<form([^>]*)>(.*?)<\/form>/g)]
          .filter((m) => m[1].includes(`${suffix}"`))
          .map((m) => ({
            attrs: m[1],
            inner: m[2],
            hidden: Object.fromEntries(
              [
                ...m[2].matchAll(
                  /<input type="hidden" name="([^"]+)" value="([^"]*)"\/>/g,
                ),
              ].map((h) => [h[1], h[2]]),
            ),
          }));
      const decisionForms = (html) => formsIn(html, "/review/decisions").length;
      const review = flat(pages.needs_review.body);
      check(
        "review: each field offers only its decisions, as named buttons in plain forms",
        /<button type="submit" class="button-secondary">Use this value<span class="visually-hidden">: 1 March 2026 for Effective date<\/span><\/button>/.test(
          review,
        ) &&
          /Use this value<span class="visually-hidden">: 1 April 2026 for Effective date<\/span>/.test(
            review,
          ) &&
          /Neither — mark unknown<span class="visually-hidden">: Effective date<\/span>/.test(
            review,
          ) &&
          /Confirm unknown<span class="visually-hidden">: Customer<\/span>/.test(
            review,
          ) &&
          /Confirm<span class="visually-hidden"> Northwind Analytics Ltd for Provider<\/span>/.test(
            review,
          ) &&
          /Mark unknown<span class="visually-hidden">: Provider<\/span>/.test(
            review,
          ) &&
          // Optional unknown fields (ambiguous end date, unparseable notice) are approved as extracted.
          !/name="field_key" value="end_date"/.test(review) &&
          !/name="field_key" value="notice_period_days"/.test(review) &&
          decisionForms(review) === 6,
      );
      check(
        "approved record: the status strip no longer says 'Needs review' once every flagged field is decided",
        text(pages.reviewed.body).includes(
          "Processing Reviewed — every flagged field is decided.",
        ) && !text(pages.reviewed.body).includes("Needs review — 2 fields."),
      );
      {
        // The workbench: every field is a link to its panel, the default panel
        // comes last (so one targeted panel can hide it), and the decision
        // forms live inside the panels.
        const keys = [
          "provider-name",
          "customer-name",
          "effective-date",
          "end-date",
          "governing-law",
          "contract-value",
          "notice-period-days",
        ];
        const panes = review.slice(review.indexOf('<div class="panes">'));
        const order = [
          ...panes.matchAll(
            /<section id="(field-[a-z-]+)" class="(pane[^"]*)"/g,
          ),
        ];
        check(
          "review: seven field links, seven panels, the default panel (the conflict) last and marked, forms inside panels",
          keys.every(
            (k) =>
              review.includes(`<a class="field-link" href="#field-${k}"`) &&
              order.some((o) => o[1] === `field-${k}`),
          ) &&
            order.length === 7 &&
            order[6]?.[1] === "field-effective-date" &&
            order[6]?.[2] === "pane pane--default" &&
            review.includes(
              '<div class="workbench" data-default="field-effective-date">',
            ) &&
            (panes.match(/<form[ >]/g) ?? []).length ===
              (review.match(/review\/decisions"/g) ?? []).length,
        );
      }
      check(
        "review: a reviewer sees one 'Review 2 fields' action (in the review band, not also in the toolbar) jumping to the first unresolved field, and the approval state",
        /<a class="button-primary band-action" href="#field-customer-name">Review 2 fields<\/a>/.test(
          review,
        ) &&
          (review.match(/>Review 2 fields</g) ?? []).length === 1 &&
          /<span class="badge">Not approved<\/span>/.test(review),
      );
      check(
        "approved record: approval is green with its words; Not scanned is amber, Blocked red (colour is never the only signal)",
        /<span class="badge badge--success">Approved, version 1<\/span>/.test(
          pages.reviewed.body,
        ),
      );
      check(
        "review: every evidence block names its source document once; the full hash is in About this result",
        (
          review.match(
            /Quoted from <span class="provenance-file">needs_review\.pdf<\/span>/g,
          ) ?? []
        ).length >= 3 &&
          /<dt>Document SHA-256<\/dt><dd><code class="full-hash">[0-9a-f]{64}<\/code>/.test(
            review,
          ),
      );
      const april = formsIn(review, "/review/decisions").find(
        (form) => form.hidden.candidate_ordinal === "1",
      );
      check(
        "review: forms post the run and the decision number the page saw (no free text)",
        april !== undefined &&
          april.attrs.includes(
            `action="${docPath("needs_review")}/review/decisions"`,
          ) &&
          april.attrs.includes('method="post"') &&
          JSON.stringify(april.hidden) ===
            JSON.stringify({
              run_id: RUN_ID,
              expected_seq: "0",
              field_key: "effective_date",
              resolution: "candidate",
              // Where a saved decision goes next: the other field that needs one.
              after: "customer_name",
              candidate_ordinal: "1",
            }) &&
          !/<input(?![^>]*type="hidden")[^>]*name=/.test(review) &&
          !/<textarea/.test(review),
        JSON.stringify(april),
      );
      const customerUnknown = formsIn(review, "/review/decisions").find(
        (form) => form.hidden.field_key === "customer_name",
      );
      check(
        "review: a decision on the last pending field, or a changed mind once nothing is pending, points at the approval; with fields pending it points at the next pending field",
        customerUnknown?.hidden.after === "effective_date" &&
          formsIn(flat(pages.reviewed.body), "/review/decisions").every(
            (form) => form.hidden.after === "approval",
          ) &&
          formsIn(flat(pages.reviewed.body), "/review/decisions").length > 0,
        JSON.stringify(customerUnknown?.hidden),
      );
      check(
        "review: approval waits for the fields that need review",
        /<div class="approval" id="approval">/.test(review) &&
          /Review needed — 2 of 7 fields:/.test(review) &&
          /Decide the fields marked Needs review to approve the record\./.test(
            review,
          ) &&
          formsIn(review, "/review/approvals").length === 0,
      );
      const reviewed = flat(pages.reviewed.body);
      check(
        "review: decisions in words; an approved current version with its download, no second approval",
        /Decided: 1 April 2026/.test(reviewed) &&
          /Decided: confirmed unknown/.test(reviewed) &&
          !/field-flag/.test(reviewed) &&
          !/field--attention/.test(reviewed) &&
          /Every field that needed review is decided · 1 of 7 fields found\./.test(
            reviewed,
          ) &&
          /Approved as version 1 · <time dateTime="2026-10-01T09:00:00.000000Z">/.test(
            reviewed,
          ) &&
          reviewed.includes(
            `<a href="${docPath("reviewed")}/review/approvals/1/export">Download JSON<span class="visually-hidden"> (version 1)</span></a>`,
          ) &&
          formsIn(reviewed, "/review/approvals").length === 0 &&
          // The chosen value is not offered again; the other one is.
          !/Use this value<span class="visually-hidden">: 1 April 2026/.test(
            reviewed,
          ) &&
          /Use this value<span class="visually-hidden">: 1 March 2026/.test(
            reviewed,
          ),
      );
      check(
        "review: 'Ready for approval' shows when a record can be approved, not while fields are pending and not once the current version is approved",
        !/Ready for approval/.test(review) &&
          !/Ready for approval/.test(reviewed) &&
          /<p class="review-title review-title--ready">Ready for approval<\/p>/.test(
            flat(pages.changed.body),
          ),
      );
      check(
        "review: a chosen value is shown as verified (green class) and a confirmed unknown as acknowledged (neutral class), in the list and in the panel",
        (
          reviewed.match(
            /class="field-decision field-decision--value">Decided: 1 April 2026</g,
          ) ?? []
        ).length >= 2 &&
          (
            reviewed.match(
              /class="field-decision field-decision--unknown">Decided: confirmed unknown</g,
            ) ?? []
          ).length >= 2 &&
          !/field-decision--null|field-decision--undefined/.test(reviewed) &&
          // Undecided flagged fields keep their amber flag and carry no decision class.
          !/field-decision/.test(review),
      );
      const changed = flat(pages.changed.body);
      const again = formsIn(changed, "/review/approvals")[0];
      check(
        "review: a decision after the approval offers a new version with the numbers seen",
        /Changed since version 1 was approved/.test(changed) &&
          again !== undefined &&
          again.attrs.includes('method="post"') &&
          JSON.stringify(again.hidden) ===
            JSON.stringify({
              run_id: RUN_ID,
              expected_seq: "3",
              expected_version: "1",
            }) &&
          again.inner.includes(">Approve again as version 2</button>"),
        JSON.stringify(again),
      );
      check(
        "review: members see decisions and versions but no forms",
        /Only reviewers can review and approve\./.test(pages.member.body) &&
          /Decided: 1 April 2026/.test(pages.member.body) &&
          decisionForms(pages.member.body) === 0 &&
          formsIn(pages.member.body, "/review/approvals").length === 0,
      );
      check(
        "review: a review of another run, or an unavailable review, offers nothing",
        decisionForms(pages.other_run.body) === 0 &&
          decisionForms(pages.review_unavailable.body) === 0 &&
          /Review is unavailable right now\./.test(
            pages.review_unavailable.body,
          ) &&
          /Review needed — 2 of 7 fields:/.test(
            flat(pages.review_unavailable.body),
          ) &&
          /Review needed — 2 of 7 fields:/.test(flat(pages.other_run.body)) &&
          !/Review is unavailable/.test(pages.other_run.body),
      );
      check(
        "review: a completed run can be approved directly (all fields approved as extracted)",
        /All required fields found · 6 of 7 fields found\./.test(
          flat(pages.completed.body),
        ) &&
          /<button type="submit" class="button-primary">Approve record<\/button>/.test(
            pages.completed.body,
          ),
      );
      const flashed = {};
      for (const code of [
        "decided",
        "approved",
        "stale",
        "unresolved",
        "bogus",
      ]) {
        flashed[code] = (
          await render(`${docPath("needs_review")}?review=${code}`)
        ).body;
      }
      // The success message reports the API's own review state for this load:
      // how many flagged fields are left, and the approved version.
      const savedReviewed = (
        await render(`${docPath("reviewed")}?review=decided`)
      ).body;
      const approvedReviewed = (
        await render(`${docPath("reviewed")}?review=approved`)
      ).body;
      const savedCompleted = (
        await render(`${docPath("completed")}?review=decided`)
      ).body;
      check(
        "review: after a decision the message says how many fields are left (or that all are decided), from the API's state; after approval, the version",
        savedReviewed.includes(
          notice("status", "Decision saved. Every flagged field is decided."),
        ) &&
          approvedReviewed.includes(
            notice("status", "Record approved as version 1."),
          ) &&
          // No flagged field at all: nothing about "left to review".
          savedCompleted.includes(notice("status", "Decision saved.")) &&
          flashed.decided.includes("2 fields left to review"),
      );
      check(
        "review: fixed messages for the route codes in a live region that is empty on load (announced when filled); unknown codes are ignored",
        flashed.decided.includes(
          notice("status", "Decision saved. 2 fields left to review."),
        ) &&
          flashed.approved.includes(notice("status", "Record approved.")) &&
          flashed.stale.includes(
            notice(
              "alert",
              "This record changed since you opened it. Nothing was saved; review the current state.",
            ),
          ) &&
          flashed.unresolved.includes(
            notice(
              "alert",
              "Decide every field marked Needs review before approving.",
            ),
          ) &&
          !/<p role="(alert|status)"[^>]*>[^<]/.test(flashed.decided) &&
          !/class="page-notice"/.test(flashed.bogus),
      );

      const decisionBody = (fields) => new URLSearchParams(fields).toString();
      const postReview = (
        state,
        what,
        fields,
        headers = { Origin: APP_ORIGIN },
        cookie = member,
      ) =>
        get(`${docPath(state)}/review/${what}`, cookie, {
          method: "POST",
          headers: {
            "Content-Type": "application/x-www-form-urlencoded",
            ...headers,
          },
          body: decisionBody(fields),
        });
      const chooseApril = {
        run_id: RUN_ID,
        expected_seq: "0",
        field_key: "effective_date",
        resolution: "candidate",
        candidate_ordinal: "1",
      };
      const reviewPostsBefore = reviewPosts.length;
      const foreign = await postReview(
        "needs_review",
        "decisions",
        chooseApril,
        {},
      );
      check(
        "decision route: no Origin is 403 and nothing reaches the API",
        foreign.status === 403 && reviewPosts.length === reviewPostsBefore,
      );
      const decided = await postReview(
        "needs_review",
        "decisions",
        chooseApril,
      );
      check(
        "decision route: the validated decision reaches the API; back to the field with decided",
        decided.status === 303 &&
          decided.headers.get("location") ===
            `${APP_ORIGIN}${docPath("needs_review")}?review=decided#field-effective-date` &&
          JSON.stringify(reviewPosts.at(-1)) ===
            JSON.stringify({
              state: "needs_review",
              suffix: "/review/decisions",
              payload: {
                run_id: RUN_ID,
                expected_seq: 0,
                field_key: "effective_date",
                resolution: "candidate",
                candidate_ordinal: 1,
              },
            }),
        `${decided.status} ${decided.headers.get("location")}`,
      );
      // "Confirm" (a found value) and "Confirm unknown" are the other two
      // decisions a reviewer makes; each must reach the API with exactly its
      // fields and come back to its own panel with the success code.
      const confirmProvider = await postReview("needs_review", "decisions", {
        run_id: RUN_ID,
        expected_seq: "0",
        field_key: "provider_name",
        resolution: "candidate",
        candidate_ordinal: "0",
      });
      check(
        "decision route: 'Confirm' reaches the API as candidate 0 of Provider and returns to that field with decided",
        confirmProvider.status === 303 &&
          confirmProvider.headers.get("location") ===
            `${APP_ORIGIN}${docPath("needs_review")}?review=decided#field-provider-name` &&
          JSON.stringify(reviewPosts.at(-1).payload) ===
            JSON.stringify({
              run_id: RUN_ID,
              expected_seq: 0,
              field_key: "provider_name",
              resolution: "candidate",
              candidate_ordinal: 0,
            }),
      );
      const confirmUnknown = await postReview("needs_review", "decisions", {
        run_id: RUN_ID,
        expected_seq: "0",
        field_key: "customer_name",
        resolution: "unknown",
      });
      check(
        "decision route: 'Confirm unknown' reaches the API as an unknown decision for Customer and returns to that field with decided",
        confirmUnknown.status === 303 &&
          confirmUnknown.headers.get("location") ===
            `${APP_ORIGIN}${docPath("needs_review")}?review=decided#field-customer-name` &&
          JSON.stringify(reviewPosts.at(-1).payload) ===
            JSON.stringify({
              run_id: RUN_ID,
              expected_seq: 0,
              field_key: "customer_name",
              resolution: "unknown",
            }),
      );
      const outcomes = {
        stale: await postReview("needs_review", "decisions", {
          ...chooseApril,
          expected_seq: "99",
        }),
        member: await postReview("member", "decisions", chooseApril),
        invalid: await postReview("needs_review", "decisions", {
          ...chooseApril,
          field_key: "end_date",
          candidate_ordinal: "0",
        }),
      };
      check(
        "decision route: stale, not a reviewer and an impossible decision are fixed codes",
        outcomes.stale.headers
          .get("location")
          ?.endsWith("?review=stale#field-effective-date") &&
          outcomes.member.headers
            .get("location")
            ?.endsWith("?review=not_reviewer#field-effective-date") &&
          outcomes.invalid.headers
            .get("location")
            ?.endsWith("?review=invalid#field-end-date"),
      );
      const postsBeforeFreeText = reviewPosts.length;
      const freeText = await postReview("needs_review", "decisions", {
        ...chooseApril,
        value: "Acme",
      });
      check(
        "decision route: a form with anything else (free text) is invalid and never reaches the API",
        freeText.headers.get("location") ===
          `${APP_ORIGIN}${docPath("needs_review")}?review=invalid` &&
          reviewPosts.length === postsBeforeFreeText,
      );
      const getDecision = await get(
        `${docPath("needs_review")}/review/decisions`,
        member,
      );
      const anonymousDecision = await postReview(
        "needs_review",
        "decisions",
        chooseApril,
        { Origin: APP_ORIGIN },
        null,
      );
      check(
        "decision route: GET is not allowed; without a session it goes to sign-in",
        getDecision.status === 405 &&
          anonymousDecision.headers.get("location") ===
            `${APP_ORIGIN}/auth/login?returnTo=${encodeURIComponent(docPath("needs_review"))}`,
      );
      const approveForm = {
        run_id: RUN_ID,
        expected_seq: "2",
        expected_version: "0",
      };
      const unresolvedApproval = await postReview(
        "needs_review",
        "approvals",
        approveForm,
      );
      const approved = await postReview("changed", "approvals", {
        ...approveForm,
        expected_seq: "3",
        expected_version: "1",
      });
      check(
        "approval route: unresolved fields and success are fixed codes at the approval panel",
        unresolvedApproval.headers.get("location") ===
          `${APP_ORIGIN}${docPath("needs_review")}?review=unresolved#approval` &&
          approved.headers.get("location") ===
            `${APP_ORIGIN}${docPath("changed")}?review=approved#approval` &&
          JSON.stringify(reviewPosts.at(-1)?.payload) ===
            JSON.stringify({
              run_id: RUN_ID,
              expected_seq: 3,
              expected_version: 1,
            }),
      );
      const exportPath = (state, version) =>
        `${docPath(state)}/review/approvals/${version}/export`;
      const exported = await get(exportPath("reviewed", 1), member);
      const exportedBytes = Buffer.from(await exported.arrayBuffer());
      check(
        "export route: the stored record as a sandboxed attachment, byte-identical",
        exported.status === 200 &&
          exported.headers.get("content-type") === "application/json" &&
          exported.headers
            .get("content-disposition")
            ?.startsWith("attachment;") &&
          exported.headers.get("x-content-type-options") === "nosniff" &&
          exported.headers.get("content-security-policy") ===
            "sandbox; default-src 'none'" &&
          exported.headers.get("cache-control") === "no-store" &&
          exported.headers.get("x-record-sha256") === sha256(RECORD_BYTES) &&
          exportedBytes.equals(RECORD_BYTES),
        JSON.stringify(Object.fromEntries(exported.headers)),
      );

      const badVersion = await get(exportPath("reviewed", "01"), member);
      const unknownVersion = await get(exportPath("reviewed", 2), member);
      const anonymousExport = await get(exportPath("reviewed", 1), null);
      check(
        "export route: malformed versions never reach the API; unknown 404; signed out to sign-in",
        badVersion.status === 404 &&
          !apiCalls.some((call) =>
            call.url?.endsWith("/approvals/01/export"),
          ) &&
          unknownVersion.status === 404 &&
          anonymousExport.headers.get("location") ===
            `/auth/login?returnTo=${encodeURIComponent(docPath("reviewed"))}`,
      );
    }

    const postProcess = (
      state,
      headers = { Origin: APP_ORIGIN },
      cookie = member,
    ) => get(`${docPath(state)}/process`, cookie, { method: "POST", headers });
    const postsBeforeProcess = processingPosts.length;
    const noOrigin = await postProcess("never", {});
    check(
      "process route: no Origin is 403 and nothing reaches the API",
      noOrigin.status === 403 && processingPosts.length === postsBeforeProcess,
    );
    const processStarted = await postProcess("never");
    check(
      "process route: starts processing and redirects to the page with started",
      processStarted.status === 303 &&
        processStarted.headers.get("location") ===
          `${APP_ORIGIN}${docPath("never")}?processing=started` &&
        processingPosts.at(-1) === "never",
      `${processStarted.status} ${processStarted.headers.get("location")}`,
    );
    const again = await postProcess("completed");
    check(
      "process route: an existing run redirects without a message (idempotent)",
      again.headers.get("location") === `${APP_ORIGIN}${docPath("completed")}`,
    );
    const off = await postProcess("start_unavailable");
    check(
      "process route: processing not configured is a fixed message",
      off.headers.get("location") ===
        `${APP_ORIGIN}${docPath("start_unavailable")}?processing=processing_unavailable` &&
        (
          await render(
            `${docPath("start_unavailable")}?processing=processing_unavailable`,
          )
        ).body.includes(
          "Processing isn&#x27;t available right now. Try again later.",
        ),
    );
    check(
      "process route: GET is not allowed",
      (await get(`${docPath("never")}/process`, member)).status === 405,
    );
    const anonymousProcess = await postProcess(
      "never",
      { Origin: APP_ORIGIN },
      null,
    );
    check(
      "process route without a session goes to sign-in",
      anonymousProcess.status === 303 &&
        anonymousProcess.headers
          .get("location")
          ?.includes("/auth/login?returnTo="),
    );
  }

  // --- A7: Content-Security-Policy and baseline headers ---
  const cspOf = (r) => r.headers.get("content-security-policy") ?? "";
  const nonceOf = (csp) => /'nonce-([A-Za-z0-9+/=]+)'/.exec(csp)?.[1];
  const pages = [
    ["home", "/", null],
    ["account", "/account", member],
    ["workspaces", list, member],
    ["workspace documents", page, member],
    ["workspace 404 page", `${list}/not-a-uuid`, member],
    ["404 page", "/no-such-page", null],
    ["tenant 404 page", "/tenants/not-a-uuid/workspaces", member],
  ];
  const seenNonces = new Set();
  for (const [label, path, cookie] of pages) {
    const response = await get(path, cookie);
    const html = await response.text();
    const csp = cspOf(response);
    const nonce = nonceOf(csp);
    seenNonces.add(nonce);
    const scripts = [...html.matchAll(/<script\b[^>]*>/g)].map((m) => m[0]);
    check(
      `${label}: CSP with a nonce, strict-dynamic, no unsafe-inline/eval`,
      Boolean(nonce) &&
        csp.includes("'strict-dynamic'") &&
        !csp.includes("unsafe-inline") &&
        !csp.includes("unsafe-eval"),
      csp,
    );
    check(
      `${label}: every script carries the response's nonce`,
      scripts.length > 0 &&
        scripts.every((tag) => tag.includes(`nonce="${nonce}"`)),
      `${scripts.length} scripts; ${scripts.find((tag) => !tag.includes(`nonce="${nonce}"`)) ?? ""}`,
    );
    // A nonce cannot authorize style="" attributes, and un-nonced <style>
    // tags are blocked; either would be a CSP violation in the browser
    // (found live: Next's built-in 404 page uses both).
    const inlineStyleAttrs = html.match(/<[a-z][^>]*\sstyle="/gi) ?? [];
    const styleTags = [...html.matchAll(/<style\b[^>]*>/gi)].map((m) => m[0]);
    check(
      `${label}: no inline style attributes, no un-nonced <style>`,
      inlineStyleAttrs.length === 0 &&
        styleTags.every((tag) => tag.includes(`nonce="${nonce}"`)),
      `${inlineStyleAttrs.length} style attributes; ${styleTags.length} <style> tags`,
    );
    check(
      `${label}: frame-ancestors 'none', object-src 'none', base-uri 'none'`,
      csp.includes("frame-ancestors 'none'") &&
        csp.includes("object-src 'none'") &&
        csp.includes("base-uri 'none'"),
    );
    check(
      `${label}: baseline headers`,
      response.headers.get("x-frame-options") === "DENY" &&
        response.headers.get("x-content-type-options") === "nosniff" &&
        response.headers.get("cross-origin-opener-policy") === "same-origin" &&
        (response.headers.get("permissions-policy") ?? "").includes(
          "camera=()",
        ),
    );
  }
  check("a fresh nonce for every response", seenNonces.size === pages.length);
  const missing = await get("/no-such-page");
  check(
    "unknown paths: status 404 with the app's own not-found page",
    missing.status === 404 && (await missing.text()).includes("Page not found"),
    String(missing.status),
  );
  const csp = cspOf(await get("/"));
  check(
    "form-action allows the issuer origin (federated sign-out redirect)",
    csp.includes("form-action 'self' https://idp.invalid"),
    csp,
  );
  check(
    "no HSTS (left to the deployment front end)",
    !(await get("/")).headers.has("strict-transport-security"),
  );
  check(
    "auth route responses carry the CSP and baseline headers too",
    Boolean(nonceOf(cspOf(login))) &&
      login.headers.get("x-frame-options") === "DENY",
  );
  const asset = /src="(\/_next\/static\/[^"]+\.js)"/.exec(listedBody)?.[1];
  const assetResponse = asset ? await get(asset) : null;
  check(
    "static assets are served without a per-request CSP",
    assetResponse?.status === 200 &&
      !assetResponse.headers.has("content-security-policy"),
    String(asset),
  );

  // --- UX1: the app shell on every page type; no inline styles anywhere ---
  {
    const procPage = `${list}/${PROC_WORKSPACE.id}/documents/${procDocuments[0].id}`;
    const shellPages = {
      home: await get("/"),
      account: await get("/account", member),
      workspaces: await get(list, member),
      workspace: await get(`${list}/${WORKSPACE.id}`, member),
      document: await get(procPage, member),
      "unknown tenant": await get(
        `/tenants/00000000-0000-7000-8000-00000000dead/workspaces`,
        member,
      ),
      "unknown path": await get("/no-such-page"),
    };
    const bodies = {};
    for (const [name, response] of Object.entries(shellPages)) {
      bodies[name] = await response.text();
      const body = bodies[name];
      // A notFound() inside a dynamic route answers 404 with Next's error
      // document and renders the not-found UI, inside the root layout, from
      // the RSC payload: there the shell is checked in the payload.
      const shell = body.includes('<html id="__next_error__">')
        ? [
            '\\"className\\":\\"skip-link\\",\\"href\\":\\"#main\\"',
            '\\"className\\":\\"app-header\\"',
            '\\"aria-label\\":\\"Main\\"',
            '\\"id\\":\\"main\\",\\"tabIndex\\":-1',
          ].every((marker) => body.includes(marker))
        : body.includes(
            '<a class="skip-link" href="#main">Skip to main content</a>',
          ) &&
          body.includes('<header class="app-header">') &&
          body.includes('<nav aria-label="Main" class="app-nav">') &&
          /<main id="main" tabindex="-1"/.test(body) &&
          (body.match(/<main[ >]/g) ?? []).length === 1;
      check(
        `${name}: skip link, header, main navigation and the skip target`,
        shell,
        String(response.status),
      );
      check(`${name}: no inline style attributes`, !/\sstyle="/.test(body));
    }
    const home = bodies.home;
    {
      const themed = async (value) =>
        (
          await (
            await get("/", undefined, {
              headers: value === null ? {} : { Cookie: `theme=${value}` },
            })
          ).text()
        ).match(/<html[^>]*>/)?.[0] ?? "";
      check(
        "theme: the server puts the visitor's choice on <html> before the first paint; no cookie or a garbled one leaves the system preference in charge",
        (await themed("light")) === '<html lang="en" data-theme="light">' &&
          (await themed("dark")) === '<html lang="en" data-theme="dark">' &&
          (await themed(null)) === '<html lang="en">' &&
          (await themed("evil")) === '<html lang="en">' &&
          (await themed("Dark")) === '<html lang="en">',
      );
      const accountBody = bodies.account;
      const railToggle =
        /<div class="rail-foot"><button type="button" class="theme-toggle theme-toggle--rail">(.*?)<\/button>/.exec(
          accountBody,
        );
      const headerToggle =
        /<button type="button" class="theme-toggle theme-toggle--header">(.*?)<\/button>/.exec(
          home,
        );
      check(
        "theme: a compact accessible toggle sits in the sidebar's utility area beside the disclaimer (header on the landing page), with a sun in dark mode, a moon in light mode and a name for each",
        railToggle !== null &&
          headerToggle !== null &&
          [railToggle[1], headerToggle[1]].every(
            (inner) =>
              /theme-state--dark"><svg[^>]*aria-hidden="true"[^>]*>.*?<\/svg><span class="theme-label">Switch to light theme<\/span>/.test(
                inner,
              ) &&
              /theme-state--light"><svg[^>]*aria-hidden="true"[^>]*>.*?<\/svg><span class="theme-label">Switch to dark theme<\/span>/.test(
                inner,
              ),
          ) &&
          /<div class="rail-foot"><button type="button" class="theme-toggle theme-toggle--rail">.*?<\/button><p class="rail-note">Synthetic documents only/.test(
            accountBody,
          ) &&
          // Account stays in the navigation, and the toggle is not in its list.
          /<nav aria-label="Primary" class="rail-nav"><ul><li>.*?href="\/account"/.test(
            accountBody,
          ) &&
          !/<nav aria-label="Primary"[^>]*>(?:(?!<\/nav>).)*theme-toggle/.test(
            accountBody,
          ) &&
          home.indexOf("theme-toggle--header") <
            home.indexOf('href="/account"'),
      );
      // Navigation is plain links (full page loads): the button must be in the
      // server's HTML and visible, never waiting for hydration.
      check(
        "theme: the toggle is never hidden until hydration (no pending state in any page)",
        !Object.values(bodies).some((body) =>
          /theme-toggle--pending/.test(body),
        ) && !/visibility:\s*hidden/.test(accountBody),
      );
    }
    {
      const hero = (html) =>
        /<p class="hero-actions"><a class="button-primary" href="\/account">([^<]*)<\/a>/.exec(
          html,
        )?.[1];
      check(
        "landing: the hero action says 'Sign in' without a session and 'Open workspace' with one, both to /account, so a signed-in visitor is not asked to sign in again",
        hero(home) === "Sign in" &&
          hero(await (await get("/", member)).text()) === "Open workspace" &&
          // A garbled or expired session cookie is not a session.
          hero(await (await get("/", "not-a-valid-session")).text()) ===
            "Sign in",
      );
    }
    check(
      "header: 'Sign in' without a session and 'Account' with one (same link, /account)",
      />Sign in<\/a><\/li>/.test(
        home.slice(
          home.indexOf('<nav aria-label="Main"'),
          home.indexOf("</header>"),
        ),
      ) &&
        !/<a href="\/account">Account<\/a>/.test(home) &&
        /<a href="\/account">Account<\/a>/.test(
          await (await get("/", member)).text(),
        ),
    );
    check(
      "home: current product copy, sign-in link, no stale 'not available yet'",
      home
        .replace(/<[^>]*>/g, "")
        .includes("Structured records you can check, value by value.") &&
        home.includes('class="button-primary" href="/account"') &&
        !home.includes("not available yet"),
    );
    // The public page promises only what exists: no live API status, and the
    // four-step workflow in plain words.
    check(
      "home: no live API status; workflow is Upload, Extract, Review, Export; says it is not deployed",
      !/API:|unreachable|not configured/.test(home.replace(/<[^>]*>/g, "")) &&
        ["Upload", "Extract", "Review", "Export"].every((step) =>
          new RegExp(`<h3>${step}</h3>`).test(home),
        ) &&
        /not deployed/.test(home),
    );
    const example =
      /<figure class="example"[\s\S]*?<\/figure>/.exec(home)?.[0] ?? "";
    check(
      "home: the example record is static synthetic content (no forms or controls)",
      example.includes("Example record · synthetic") &&
        example.includes("<mark>1 March 2026</mark>") &&
        !/<(form|button|input|select|textarea)\b/.test(example) &&
        !/%|confidence/i.test(example),
    );
    const iconTag = /<link rel="icon"[^>]*>/.exec(home)?.[0] ?? "";
    const icon = iconTag.includes('type="image/svg+xml"')
      ? /href="(\/icon\.svg[^"]*)"/.exec(iconTag)?.[1]
      : undefined;
    const iconResponse = icon ? await get(icon.replaceAll("&amp;", "&")) : null;
    check(
      "the icon is linked and served as SVG",
      iconResponse?.status === 200 &&
        iconResponse.headers.get("content-type")?.startsWith("image/svg+xml"),
      iconTag,
    );
  }

  check(
    "server log contains no tokens or secrets",
    ![
      "good-token",
      "revoked-token",
      SECRET,
      "not-a-real-secret",
      "Quarterly report",
      "Seeded",
    ].some((s) => serverLog.includes(s)),
  );
} finally {
  server.kill("SIGTERM");
  api.close();
}

if (failures.length > 0) {
  console.error(`check-auth-runtime: ${failures.length} failed`);
  process.exit(1);
}
console.log("check-auth-runtime: all checks passed");

async function freePort() {
  const probe = createServer();
  probe.listen(0, "127.0.0.1");
  await once(probe, "listening");
  const { port } = probe.address();
  probe.close();
  return port;
}

async function waitForServer(url) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    try {
      await fetch(url, { redirect: "manual" });
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
  }
  throw new Error(`next start did not come up:\n${serverLog}`);
}
