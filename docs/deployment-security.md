# Deployment security requirements

Controls that belong to the production front end (reverse proxy, load balancer, CDN or WAF) and to the scanner and parser runtime, not to the application code. The application does not implement rate limits by design: an in-process limiter would be per instance, reset on every restart, and give a false sense of protection.

**The project has not been deployed. Nothing marked "Required, not done" below exists.** Each such item must be implemented and verified before any public deployment, with the evidence linked in the deployment change. Owner: the repository owner.

| Item | Status |
| --- | --- |
| Edge rate limits, body limits and slow-client timeouts (section 1) | Required, not done |
| Front end that does not break early upload answers (section 2) | Required, not done; the application's behaviour is verified locally |
| Parser runner that does not give the worker the Docker socket (section 2) | Required, not done: release blocker. The sandbox contract itself is verified locally and in CI through the Docker CLI |
| Scanner deployment: internal-only access, configuration, signature updates, monitoring, capacity, per-tenant upload limits (section 3) | Required, not done. Scan behaviour is verified locally and in CI |
| HTTPS only and HSTS (section 4) | Required, not done |
| API documentation endpoints (`/docs`, `/redoc`, `/openapi.json`) not served (section 1) | Implemented: off unless `API_DOCS=1`; verify it is unset in the deployment |
| Application headers and CSP, fail-closed scanning, per-document sandbox, upload authorization before the body is read (sections 2 and 5) | Implemented and tested locally and in CI; not tested behind a production front end |

## 1. Rate limiting (required before public deployment)

Limits are per client IP address, applied at the edge before requests reach the web or API processes. Values are starting points to tune against real traffic; each limit returns `429` with `Retry-After` and is logged at the edge.

| Path | Why | Starting limit |
| --- | --- | --- |
| `GET /auth/callback` (web) | Each request with a valid sealed transaction cookie makes the web server call the provider's token endpoint. The cookie is stateless and can be replayed for up to 10 minutes, so an attacker could drive many token-endpoint calls and exhaust the provider's rate limit for everyone (login denial of service). | 10 per minute per IP |
| `GET /auth/login` (web) | Each request performs provider discovery (cached) and sets a transaction cookie; cheap, but the entry point for the above. | 30 per minute per IP |
| `POST /auth/logout`, `POST /tenants/*/workspaces/create` (web) | State-changing; normal use is a few per minute. | 30 per minute per IP |
| `POST /tenants/*/workspaces/*/documents/upload` (web) and `POST /v1/tenants/*/workspaces/*/documents` (API) | An authorized member's request can carry up to 20 MiB that the server reads in full; non-members are refused before any of the body is read (see section 2). | 10 per minute and 200 MiB per hour per IP |
| `GET /tenants/*/workspaces/*/documents/*/download` (web) and `GET /v1/…/documents/*/content` (API) | Each request streams up to 20 MiB from the database. | 60 per minute per IP |
| All other web routes | General flood protection. | 300 per minute per IP |
| API (`/v1/*`) | Token verification and database work per request. Reachable only from the web server's network in the intended deployment; if the API is publicly reachable, apply this limit at its edge. | 600 per minute per IP |
| API `/health/*` | Unauthenticated; must stay available to the orchestrator. Restrict to internal networks rather than rate-limit. | internal only |

Also required:
- Request body limits at the edge at or below the application's own: 8 KiB for the create form and the API's create endpoint; 20 MiB + 64 KiB (20,037,056 bytes) for the web upload route (one file of at most 20 MiB plus multipart framing); 20 MiB (20,971,520 bytes) for the API's document upload. Everything else: 8 KiB. The edge must not buffer or rewrite upload bodies in a way that truncates them.
- Timeouts for slow clients (headers and body), so slow requests cannot hold connections open.
- The API's interactive documentation and OpenAPI schema (`/docs`, `/redoc`, `/openapi.json`) need no authentication, so they are off by default and only `API_DOCS=1` turns them on (the local make targets set it). Never set it in a deployment; if the API is publicly reachable, also block these paths at the edge.
- The provider's own protections stay enabled (Auth0 brute-force and suspicious-IP throttling).

Verification (record the evidence in the deployment change):
1. For each row, a scripted burst from one IP above the limit returns `429` with `Retry-After`, while a second IP is unaffected.
2. A normal sign-in, workspace creation, document upload and download, and sign-out still work under the limits.
3. The edge logs show the rejected requests without request bodies, cookies or `Authorization` headers.

## 2. Documents: limits, routing and malware scanning

**Limits.** One document is at most 20 MiB (20,971,520 bytes) and must start with the PDF signature `%PDF-`; the API enforces both (`413 too_large`, `415 unsupported_media_type`) and validates the name (`422 invalid_name`). The web upload route refuses a declared or streamed body over 20 MiB + 64 KiB and a file over 20 MiB before calling the API, and otherwise relays the API's decision as a fixed code. Nothing is parsed, rendered, previewed or extracted.

**Routing.** The web upload route (`/tenants/{t}/workspaces/{w}/documents/upload`) and download route (`…/documents/{d}/download`) are excluded from `src/proxy.ts`:
- Upload: with a proxy, Next.js buffers request bodies up to `proxyClientMaxBodySize` (default 10 MB) and silently truncates the rest, which would corrupt larger uploads. The runtime check (`scripts/check-auth-runtime.mjs`) uploads 12 MiB through `next start` and requires byte-identical arrival at the API; re-adding the route to the proxy fails it.
- Download: the proxy's page policy would replace the route's own `Content-Security-Policy: sandbox; default-src 'none'`. The response is always `Content-Disposition: attachment` (the route refuses anything else with 502), `application/pdf`, `nosniff` and `no-store`.

Both routes keep the baseline headers from `next.config.ts`. The front end must pass the download headers through unchanged and must not cache either route.

**Early answers to uploads (required front-end behaviour).** The upload route can answer before it has read the request body: a declared body over the limit (`303 …?error=too_large`), a foreign or missing `Origin` (`403`), or no session (redirect to sign-in). `next start` then reads and discards the rest of the body and closes the connection cleanly (verified locally). The front end must not break this. It must either receive the whole body before forwarding (within the body limit in section 1), or keep reading and discarding the client's remaining body after an early upstream answer. It must never close a connection while client data is still unread: the operating system then resets it and browsers show `ERR_CONNECTION_RESET` instead of the answer (observed with a local test forwarder, fixed there by draining). Verification: through the front end, a browser upload of a synthetic 21 MiB PDF shows "The file is larger than 20 MiB." next to the file field, with no connection error.

**Malware scanning (implemented and tested locally and in CI; deployment requirements in section 3).** Every upload is scanned by ClamAV (`clamd`) before anything is stored, and nothing is stored or downloadable unless the verdict is clean (`apps/api/README.md`, "Malware scanning"). Scanner errors, outages and signatures older than 72 hours fail closed (503 `scan_unavailable`). What remains a release blocker:
1. **The scanner deployment itself** (section 3): every item there must be implemented and verified before public deployment.
2. **Parser runtime.** A clean upload verdict is not proof of complete inspection: ClamAV can pass content it did not fully inspect, and it neither flags nor inspects permissions-only encrypted PDFs (the API's `/Encrypt` check rejects those). Documents are therefore parsed only in an isolated sandbox.
   - **Verified locally and in CI:** the isolated parser (`apps/parser`, run by `apps/api/src/api/sandbox.py`) is used by the processing worker (`apps/api/src/api/worker.py`, role `app_worker`), which reads a document's bytes only for the run it holds and only while the latest verdict is clean. The worker runs the parser image by its exact id (`PARSER_IMAGE_ID`). Each document is parsed in its own container: no network, a read-only root, no capabilities, an unprivileged user, CPU, memory, process, time and output limits, and fail-closed outcomes. Embedded files and attachments are refused rather than inspected, and only the page text layer is read.
   - **Deployment requirement (release blocker, not done):** the development and CI runner starts containers with the Docker CLI. A deployed worker must **not** get the Docker socket, which is equivalent to root on the host. The deployment must provide the same per-document isolation contract through a runtime that doesn't hand the worker host control, for example a gVisor- or microVM-backed job runner, and must pass `apps/api/tests/test_parser_sandbox.py`'s isolation probes against it.
3. **Sharing documents** beyond the workspace members who can already upload them needs its own review.

Record the scanner image or version, the signature update process and the verification evidence in the deployment change.

**Upload authorization happens before the body is read (implemented and tested).** A signed-in user who is not a member of the tenant, or who names a workspace they cannot see, is refused with the uniform `404` before any of the upload body is read. The web route asks the API for the workspace (no body) first. The API's upload endpoint authorizes membership and the workspace before its body reader runs, and the insert checks both again (row-level security). Tests count body reads and require zero for every refused case, with and without `Content-Length`. Because the workspace is decided first, an unknown, foreign or non-member workspace gets `404` even when the upload is also oversized, not a PDF or badly named.

"Not read" means the application never consumes the body; the HTTP server or reverse proxy may still receive and discard the remaining bytes. Network ingress is therefore not prevented, and these risks remain: an authorized member can send up to 20 MiB per request; a refused request can still bring that much ingress; every refused attempt costs an API lookup or a few database queries. The rate and volume limits in section 1 stay required.

## 3. Malware scanner (clamd): deployment and operations

This is the checklist the deployment change must implement and verify. It is not provider-specific: choose the mechanism when a deployment target exists.

**Connectivity: internal only.**
- clamd must be reachable **only by the API**: preferably a Unix socket shared with the API process, as a sidecar or on the same host (`CLAMD_ADDRESS=unix:///path/clamd.sock`). Otherwise a private network address (`tcp://host:3310`), with the port closed to everything else.
- clamd has no authentication or TLS. Anything that can reach it can make it scan and read its version.
- **Verification:**
  - the port or socket is not reachable from outside the API's network or host;
  - `GET /health/scanner` on the API answers 200.

**Configuration.**
- Use the verified settings in `apps/api/tests/clamav/clamd.conf`:
  - `AlertExceedsMax yes` and `AlertEncrypted yes`. Both are off by default in clamd, and without them exceeded limits and encrypted documents or archives pass as clean;
  - `StreamMaxLength 21M`, `MaxFileSize 25M`, `MaxScanSize 100M`, `MaxRecursion 17`, `MaxScanTime 20000`.
- **Engine version:** pin the image or version. Upgrade only after the real-engine CI suite passes on the new pin.
- **Capacity:**
  - `MaxThreads` at least the sum of the API processes' concurrent scans (4 per process);
  - CPU and memory limits for clamd, sized from a measurement: the signature database needs on the order of a gigabyte. Measure it; don't assume.
- **Verification:** `clamconf` or the startup log shows these values.

**Signature freshness.**
- **Updates:** `freshclam` on a schedule (the official mirror or a private mirror), at least a few times a day.
- **The API's rules (already implemented):** it refuses uploads when signatures are older than **72 hours** (503 `scan_unavailable`, reason `signatures_stale`), and logs a warning after **24 hours**.
- **Alerting:** alert when signatures pass 24 hours, and on any `upload refused: scan_unavailable (signatures_stale)` log line.
- **Verification:** stop updates in a staging environment. The warning appears after 24 hours, and uploads refuse after 72 hours; or simulate it with an old signature database.

**Health and monitoring.**
- **`GET /health/scanner`** (API; internal only, like `/health/*`) answers 200 `{"status":"ok"}` only when clamd answers and its signatures meet the upload rules, and 503 `{"status":"unavailable"}` otherwise.
  - It uses the same client and freshness rule as uploads, and scans nothing.
  - The body carries no versions, addresses or dates.
  - `/health/ready` does **not** depend on the scanner: a scanner outage stops uploads, not reads or downloads.
- **Monitoring:** poll `/health/scanner`, and alert on sustained 503.
- **Log categories to count** (only categories and detection names are logged):
  - `upload refused: scan_unavailable (<reason>)`, where the reason is `timeout`, `transport`, `reply`, `busy`, `signatures_stale`, `signatures_unknown` or `not_configured`;
  - `upload refused: malware_detected (<name>)`, `scan_incomplete (<name>)`, `encrypted_pdf (…)`;
  - `scanner signatures are N hours old`.

**Time, body, rate and volume at this boundary** (in addition to section 1).
- The API gives a scan 30 s (2 s to connect); the web route gives the API upload 60 s. The front end's request timeout for the upload route must exceed both, so it never cuts an upload that is still being scanned.
- **Upload volume:** every upload now costs scanner CPU. Keep the section 1 per-IP upload limits, and add a **per-tenant** upload rate and volume limit (starting point: 60 uploads and 500 MiB per tenant per hour). When 4 scans are already running, a process answers 503 `scan_unavailable (busy)`; alert on its rate.
- **Verification:**
  - a burst above the limits gets 429 at the edge;
  - a 20 MiB upload completes through the front end;
  - the scanner-busy rate is visible in the logs.

**Rollback and operations.**
- **Scanner outage:** uploads get 503, and reads and downloads keep working. Restore clamd; there's nothing to repair in the database, because nothing was stored.
- **Stale signatures:** fix the updates; uploads resume automatically once the signatures are within 72 hours.
- **Rolling back migration 0007** drops `document_scans`. Re-applying it leaves every version `not_scanned`, so downloads are blocked until a separately approved rescan. Don't roll back 0007 in a deployment with real uploads unless that's acceptable.
- **Rolling back the application** to before scanning while 0007 is applied: uploads fail at the database (the commit-time check), so roll back the migration too, with the consequence above.

**Local development and live checks.**
- `make api-scanner-up` runs the CI-pinned image through Docker on 127.0.0.1:3310 only, with the test configuration and a test-only marker signature, and updates its signatures once with `freshclam`.
  - Without Docker it stops, changes nothing, and explains.
  - It never installs anything or touches system services.
- `make api-live-scan-check` exercises every scan outcome against a running API.
- Details: `apps/api/README.md`.

## 4. Transport security

- HTTPS only, with a valid certificate; plain HTTP redirects to HTTPS.
- `Strict-Transport-Security: max-age=31536000; includeSubDomains` set by the HTTPS front end once the production domain is final (not by the application: on `localhost` it would force HTTPS for every local project). Add `preload` only as a separate, deliberate decision.
- Verification: `curl -sI https://<host>/` shows the HSTS header; `curl -sI http://<host>/` redirects to HTTPS.

## 5. Already provided by the application (for reference)

The web app sets, on every response: a per-request nonce Content-Security-Policy (`src/proxy.ts`, including `frame-ancestors 'none'`; the document routes set their own, see section 2), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Permissions-Policy`, `Cross-Origin-Opener-Policy: same-origin`; `Cache-Control: no-store` on authenticated pages and auth routes. The front end must pass these through unchanged and must not add caching for `/auth/*`, `/account` or `/tenants/*`.

Known residual: Next.js's static `500.html` fallback (built at build time) cannot carry a per-request CSP nonce. It is served only when rendering fails outside React; its styling is then blocked by the policy while the text stays readable. The front end may replace upstream 5xx pages with its own static error page instead.
