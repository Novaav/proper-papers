# API

FastAPI service. Requires [uv](https://docs.astral.sh/uv/); uv provisions Python 3.14.

From the repository root:

```sh
make api-setup   # install locked dependencies
make api-db-migrate  # apply migrations as app_owner (DATABASE_URL_OWNER)
make api-check   # format, lint, strict types, then on a freshly recreated test database:
                 # migrations, drift (`alembic check`), tests (needs `make api-db-up`)
make api-run     # serve on http://127.0.0.1:8000 (with the interactive docs at /docs; they are off unless API_DOCS=1)
```

The local PostgreSQL 18 cluster (`make api-db-install`, `api-db-up`, `api-db-down`) is described in the repository README. `db/roles.sql` defines the database roles (once per cluster) and `db/database.sql` one database with its schema `app` and grants; locally `scripts/local-postgres.sh` runs them, in CI the workflow runs them against the `postgres` service container.

## Endpoints

| Method | Path           | Purpose                                              |
| ------ | -------------- | ---------------------------------------------------- |
| GET    | `/health/live` | Liveness: the process is up and serving HTTP. Checks no dependencies and runs no SQL. |
| GET    | `/health/ready` | Readiness: `SELECT 1` succeeds through the runtime engine. 200 `{"status": "ready"}`, otherwise 503 `{"status": "unavailable"}` (no error details). |
| GET    | `/health/scanner` | Internal operational check: 200 `{"status": "ok"}` only when the malware scanner answers and its signatures meet the upload rules (the same client and 72-hour rule as uploads; nothing is scanned); otherwise 503 `{"status": "unavailable"}`. No versions, addresses or dates. Readiness does not depend on it. |
| GET    | `/v1/me` | Requires `Authorization: Bearer <access token>`. Returns `{"user_id", "memberships": [{"tenant_id", "role"}]}` for the verified user, creating the user on first call. 401 `{"detail": "unauthorized"}` for missing or invalid tokens; 503 `{"detail": "unavailable"}` if authentication is not configured or the database is unavailable. Every response carries `Cache-Control: no-store`. |
| GET    | `/v1/tenants/{tenant_id}/workspaces` | The tenant's workspaces: 200 `{"workspaces": [{"id", "name", "created_at"}]}`, oldest first (ties by id). No pagination yet. |
| POST   | `/v1/tenants/{tenant_id}/workspaces` | Body `{"name": "<1 to 200 characters after trimming spaces>"}` (no other fields). 201 with the workspace and `Location: /v1/tenants/{tenant_id}/workspaces/{id}`. |
| GET    | `/v1/tenants/{tenant_id}/workspaces/{workspace_id}` | One workspace of the tenant: 200 `{"id", "name", "created_at"}`. |
| POST   | `/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents` | Upload a PDF as a new document: raw body (`Content-Type: application/pdf` or `application/octet-stream`), name in `X-Document-Name` (percent-encoded UTF-8). 201 with the document and `Location`. 413 `too_large`, 415 `unsupported_media_type`, 422 `invalid_name`, 422 `encrypted_pdf`, 422 `malware_detected`, 422 `scan_incomplete`, 503 `scan_unavailable` (no acceptable scan verdict; distinct from the database's 503 `unavailable`). Nothing is stored unless the scan is clean. |
| GET    | `/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents` | The workspace's documents, newest first: `{"documents": [{"id", "workspace_id", "name", "created_at", "version": {"id", "number", "media_type", "size_bytes", "sha256", "scan_status"}}]}`; `scan_status` is `clean`, `not_scanned` or `blocked`. |
| GET    | `/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents/{document_id}` | One document's metadata. |
| GET    | `/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents/{document_id}/content` | The original bytes as an attachment (never inline); only for `clean` versions, otherwise 409 `not_scanned` or 409 `blocked`. |
| POST   | `/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents/{document_id}/processing` | Queue the document for processing (no body): 202 `{"run": {...}}` for a new run, 200 with the existing run of the same pipeline version (whatever its status; a failed run is not queued again). 409 `not_scanned` / `blocked` unless the latest verdict is clean; 503 `processing_unavailable` when `PARSER_IMAGE_ID` is not configured. |
| GET    | `/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents/{document_id}/processing` | The newest run and, once `completed` or `needs_review`, its `service_agreement.v1` result with exact evidence: 200 `{"run": {...} or null, "result": {...} or null}`. 409 `not_scanned` / `blocked` unless the latest verdict is clean. |
| GET    | `/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents/{document_id}/review` | The review state (see "Review"): 200 `{"run_id", "can_review", "decisions_through", "decisions": [...], "approvals": [...]}`; `run_id` is null until the newest run completed or needs review. 409 `not_scanned` / `blocked` unless the latest verdict is clean. |
| POST   | `/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents/{document_id}/review/decisions` | Owners and reviewers only (403 `not_reviewer`). Body `{"run_id", "expected_seq", "field_key", "resolution": "candidate" or "unknown", "candidate_ordinal"}`. 201 with the decision. 409 `stale` / `run_changed`, 422 `invalid_decision` / `invalid_request`. |
| POST   | `/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents/{document_id}/review/approvals` | Owners and reviewers only. Body `{"run_id", "expected_seq", "expected_version"}`. 201 with the new version. 422 `{"detail": "unresolved_fields", "fields": [...]}`, 409 `stale` / `run_changed`, 422 `invalid_request`. |
| GET    | `/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents/{document_id}/review/approvals/{version}/export` | The stored `approved_record.v1` JSON of one version as an attachment (`nosniff`, `sandbox` CSP, `X-Record-SHA256`), byte-identical on every call; 404 for an unknown version. |

The workspace endpoints require a token like `/v1/me` (401, 503 as above) and a membership (`owner`, `reviewer` or `member`; all may list, create and get) in `tenant_id`. A tenant the caller is not a member of, an unknown tenant or workspace, another tenant's workspace, and a malformed ID all get the same 404 `{"detail": "not_found"}`. IDs must be canonical lowercase UUIDs; other spellings are not found. A missing, malformed, oversized (over 8 KiB) or invalid create body gets 422 `{"detail": "invalid_name"}`, checked only after authentication and membership, so callers without access never see a 422. Every response carries `Cache-Control: no-store`. There are no rename or delete endpoints.

`make api-run` needs `DATABASE_URL` (the `app_runtime` URL, e.g. `DATABASE_URL_RUNTIME` from `.local/postgres/credentials.env`). The engine is created at startup and disposed at shutdown; nothing connects at import or startup, and migrations never run automatically. If `DATABASE_URL` is missing or invalid, the API still starts, logs the reason (never the URL) and reports 503 from `/health/ready`: a misconfigured process is live but not ready, so anything routing traffic to it must use readiness. Timeouts: 2 s to connect, 2 s to get a pooled connection, 1 s for the readiness query.

Readiness limits: it checks connectivity and the runtime role only, not that migrations are at the latest revision. The timeouts above are enforced by libpq and PostgreSQL; a server that stops responding after a connection is established (a hung process or a network partition) is not bounded by them, because libpq has no client-side read timeout.

Database tests are mandatory and run only against the disposable test database: they read `TEST_DATABASE_URL_OWNER`, `TEST_DATABASE_URL_RUNTIME` and `TEST_DATABASE_URL_ADMIN` (from `.local/postgres/test.env` locally, exported by CI), verify that each points at a database named `app_test` whose comment is `disposable-test-database`, and fail, rather than skip, otherwise. The admin (superuser) URL is used only to create and drop temporary roles for the runtime role guard tests. `/health/live` does not use the database.

## Authentication

`src/api/auth.py` verifies access tokens; nothing else establishes identity. Configuration (read at startup; no secrets involved):

| Variable | Meaning |
| --- | --- |
| `AUTH_ISSUER` | The provider's issuer, exactly as it appears in tokens (https only), e.g. `https://<tenant>.eu.auth0.com/` |
| `AUTH_AUDIENCE` | The API identifier tokens must be issued for |
| `AUTH_ALLOWED_CLIENT_IDS` | Comma-separated client IDs whose tokens are accepted (our web app) |

A token is accepted only if all of these hold: exactly one `Authorization: Bearer` header; header `alg` is `RS256` and `typ` is `at+jwt` (RFC 9068; ID tokens are refused); `kid` names a key in `<AUTH_ISSUER>.well-known/jwks.json`, the only key source (`jku`, `x5u` and embedded keys are ignored); valid signature; `iss` equals `AUTH_ISSUER` exactly; `aud` is or contains `AUTH_AUDIENCE`; `client_id` is allowed; `sub` is 1 to 255 characters; `exp`, `iat` and `nbf` hold with 30 seconds of leeway. Keys are fetched on first use (2 s timeout), cached for 10 minutes, and an unknown `kid` refetches at most once a minute; if the key set cannot be fetched, tokens are rejected. Rejections return a fixed 401 body; only a reason category is logged, never the token. Without valid `AUTH_*` settings the API still starts and `/v1/me` returns 503; the health endpoints never use authentication.

`src/api/identity.py` runs one transaction as the verified identity: it sets `app.auth_issuer`/`app.auth_subject`, creates the user on first sight (`INSERT … ON CONFLICT DO NOTHING`, allowed by row-level security only for that identity), and sets `app.user_id` so only the user's own memberships are visible.

`src/api/authorization.py` turns a request into a `TenantContext(user_id, tenant_id, role)`: in one transaction it looks up the caller's membership in the path's tenant (row-level security shows only the caller's own memberships) and returns no context, hence 404, if there is none. It is the only module that creates a `TenantContext` (a test enforces this). The workspace operation then runs in a second transaction through `tenant_transaction`, where row-level security checks the membership again: if it was revoked in between, reads return nothing and a create is rejected (404).

## Workspace repository

`src/api/workspaces.py` provides `create_workspace`, `list_workspaces` and `get_workspace`. Each takes the `Session` from `tenant_transaction` and never commits or opens transactions of its own. None takes a tenant: new workspaces get `tenant_id` from the transaction's `app.tenant_id` inside the database, and row-level security limits reads to that tenant, so another tenant's workspace and a nonexistent one both return `None`. Called outside `tenant_transaction` (without both a user and a tenant), they raise `TenantContextMissing`. Names are validated like the database constraint: 1 to 200 characters after trimming spaces. Known limitation: like the constraint's `btrim()`, only spaces are trimmed, so names made only of tabs, newlines or non-breaking spaces are accepted; tightening this is deferred and must change the constraint and the validation together.

## Documents

`src/api/documents.py` stores uploaded PDFs; the endpoints above expose it. Uploaded files are **untrusted bytes**: they are checked, malware-scanned, hashed, stored and returned, and never parsed, rendered or processed by the API (no PDF library is used). Scanning is not a substitute for isolating any future parsing (see Malware scanning).

- **Limits:** 1 byte to 20 MiB (20,971,520 bytes) per file, one file per request. A declared `Content-Length` over the limit is refused without reading; the streamed body is counted too; the repository and database constraints enforce the same bound.
- **Type:** only PDFs. The declared `Content-Type` must be `application/pdf` or `application/octet-stream`, but it is only a hint: the bytes must start with `%PDF-`, and the stored media type is always set by the server.
- **Names:** display text only, never a path or storage key: NFC-normalised, directory parts and surrounding spaces removed; empty names, names over 255 characters, and control, bidirectional-override or zero-width characters are rejected.
- **Storage:** PostgreSQL. `documents` (workspace, name, author), `document_versions` (immutable upload record: version number, media type, size, SHA-256, author) and `document_contents` (the bytes, `bytea`). Composite `(tenant_id, …)` foreign keys keep every row inside one tenant. The document, its version and its bytes are inserted in one transaction, so a failed upload leaves nothing.
- **Isolation:** every request verifies the token and the caller's membership, and row-level security re-checks the membership on every statement (runtime reads and inserts only for the transaction's tenant and user; inserts only as the transaction's user). The runtime role has SELECT and INSERT only (no UPDATE, DELETE or TRUNCATE), so stored versions and bytes cannot be changed; the operator role has no access. Unknown, foreign or malformed tenant, workspace and document IDs, and documents under the wrong workspace, all answer the same 404.
- **Uploads are authorised before the body is read:** the caller must be a member of the tenant and able to see the workspace; otherwise the uniform 404, without the application reading any of the body. This means an unknown, foreign or non-member workspace is 404 even when the upload is also oversized, not a PDF or badly named (before D-058, a member's unknown workspace got 413/415/422 first). Status codes and response bodies are unchanged. The insert checks both again (row-level security), so a membership revoked in between still stores nothing.
- **Downloads:** authorised on every request; `Content-Disposition: attachment` (RFC 6266 `filename*`), `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox; default-src 'none'`, `Cache-Control: no-store`; whole files only (no range requests). Downloads are not audited; the version row records the upload.
- **Logs:** never contain names, content or hashes; only categories (and, for a refused upload, the scanner's detection name).

## Malware scanning

Every upload is scanned by ClamAV (`clamd`) before anything is stored: after authorization (D-058) and the cheap checks, before the database transaction. `src/api/scanning.py` is a small client for clamd's INSTREAM protocol (standard library only), configured by `CLAMD_ADDRESS` (`tcp://host:port` or `unix:///path`).

- **Fail closed:** only an exact `stream: OK` reply is clean. Every other outcome stores nothing and has a fixed answer:
  - a detection → 422 `malware_detected`;
  - an exceeded scan limit (`Heuristics.Limits.Exceeded*`) → 422 `scan_incomplete`;
  - an encrypted document (`Heuristics.Encrypted*`) → 422 `encrypted_pdf`;
  - anything else → 503 `scan_unavailable`: an ERROR or unexpected reply, a timeout (30 s per scan, 2 s to connect), a refused or dropped connection, more than 4 concurrent scans in the process, an unknown signature version, or signatures older than 72 hours (a warning is logged after 24).
  - Without `CLAMD_ADDRESS`, or with an invalid one, the API starts, logs the reason and answers every upload with 503. There is no way to skip scanning.
- **Encrypted PDFs** are rejected (422 `encrypted_pdf`), including owner-password-only ones:
  - **the API's byte check** for the `/Encrypt` name token (also hex-escaped, e.g. `/Encr#79pt`), before the scan. It covers **the document's own encryption when declared as the PDF specification requires** (the key sits in an uncompressed trailer or cross-reference stream dictionary; indirect references, incremental updates and cross-reference streams included). It is a byte pattern, not a PDF parser, and **not a general detector**: it does not see encryption inside embedded payloads (compressed attachments such as PDFs or password-protected archives) or in malformed files that a reader repairs;
  - **the scanner's encryption heuristics** (clamd `AlertEncrypted`: encrypted documents and archives). According to ClamAV's documentation this also covers embedded encrypted archives that the engine extracts; **this PR's real-engine suite does not verify the embedded-archive case**. With ClamAV 1.4.6 it flags PDFs that need a password to open. It does **not** flag permissions-only PDFs (empty user password), **and it does not inspect their encrypted content either**: the real-engine tests show a known test signature inside such a file going undetected, with a clean verdict.

  So, for permissions-only encrypted PDFs, **the byte check is the only control that rejects them**, and a clean engine verdict on an encrypted PDF is not evidence that its content was inspected. A file whose encryption the byte pattern misses (an embedded payload, or a structure a reader repairs) and that the engine does not flag would be stored. **Future processing must therefore independently inspect extracted and embedded content inside the isolated processing sandbox, and fail closed** (encrypted or uninspectable content is not processed); the upload scan is not the final guarantee.
- **Record and invariants (migration 0007):** each version has append-only verdicts in `document_scans`, ordered by `scan_number`; the latest verdict decides.
  - The database refuses to commit a version without scan 1 = clean (a deferred constraint trigger). This applies to every role that inserts rows, the runtime and the owner alike. Only the schema owner (the migration role) can suspend the trigger, and only through DDL (`ALTER TABLE … DISABLE TRIGGER`).
  - Row-level security lets the runtime read a version's bytes only while its latest verdict is clean.
  - The runtime may insert only scan 1 = clean, as the transaction's user; nothing can be updated or deleted; the operator has no access.
  - Versions stored before 0007 have no verdict: listed as `not_scanned` and not downloadable (409) until a separately approved rescan; the migration adds and deletes nothing.
- **What a clean verdict means:** no known signature matched in the content ClamAV inspected. It is not proof that every byte was inspected. clamd must run with `AlertExceedsMax` and `AlertEncrypted` (off by default, so exceeded limits and encrypted documents or archives would otherwise pass as clean), and even then an open ClamAV issue reports an exceeded limit passing as clean. The real-engine tests (`tests/test_clamd_real.py`, mandatory in CI against a digest-pinned image, configurations in `tests/clamav/`) pin the behaviour for the cases they construct: clean, EICAR, a test-only signature, a file and a decompressed stream beyond the limits, password-protected and permissions-only encrypted PDFs, and an oversized stream. Any future parsing must still run isolated.

### Local scanner and live check

- **Local scanner (Docker only):**
  - `make api-scanner-up` runs the CI-pinned ClamAV image on **127.0.0.1:3310 only**, with `tests/clamav/clamd.conf` and the test-only marker signature. It downloads current official signatures once with `freshclam`, because the API refuses signatures older than 72 hours.
  - `make api-scanner-status`, `api-scanner-update` and `api-scanner-down` are the other commands; `scripts/local-clamav.sh remove` deletes the container.
  - The pinned image exists for `linux/amd64` only; the helper requests that platform explicitly, so on Apple Silicon it runs under Docker's emulation (slower to start, same behaviour).
  - It acts only on its own labelled container. Without Docker it stops with instructions and changes nothing. It never installs software or touches system services.
- **API settings:** `make api-run` reads optional settings from the gitignored `.local/api.env` (e.g. `CLAMD_ADDRESS=tcp://127.0.0.1:3310`, `DATABASE_URL`, `AUTH_*`).
- **Live check:** `make api-live-scan-check` (`scripts/live_scan_check.py`) runs the scan checks against a running API with a member's access token: `LIVE_ACCESS_TOKEN`, `LIVE_TENANT_ID`, `LIVE_WORKSPACE_ID`, `LIVE_FOREIGN_TENANT_ID`, `LIVE_FOREIGN_WORKSPACE_ID`; optional `LIVE_API_BASE_URL`, and `LIVE_SCANNER_OUTAGE=1` to stop and restart the local scanner. It checks, in order:
  1. scanner health;
  2. a clean upload stored as clean and downloaded byte-identical;
  3. the marker PDF refused;
  4. an encrypted PDF refused;
  5. legacy documents answering 409;
  6. another tenant answering 404;
  7. optionally, the scanner outage (503, nothing stored).

  It uses synthetic files only and sends only GET and POST: nothing is deleted, and the one clean upload stays. It stops at the first failure. Browser, keyboard and screen-reader checks stay manual.

## Parser sandbox (DI-1)

`src/api/sandbox.py` is the only way document bytes will be parsed. `parse_in_sandbox(content, settings)` starts one container per document from the parser image (`apps/parser`), with no network, a read-only root, no capabilities and resource limits. It returns validated pages and flags or a fixed failure code, and never raises for document problems. There is no fallback: this package has no PDF library, and an unavailable sandbox is `sandbox_unavailable`, the only transient code. Document text never reaches the logs. The processing worker (below) is its only caller. Details, limits and the isolation properties: [`apps/parser/README.md`](../parser/README.md).

- **Tests:** `tests/test_parser_sandbox.py` has unit tests (no Docker) and real-sandbox tests: the whole corpus, isolation probes run with the same flags, timeouts, memory, output cap and malformed output. The real-sandbox tests need `PARSER_IMAGE` (`make parser-image`). They are mandatory under GitHub Actions or with `REQUIRE_PARSER_SANDBOX=1`, and skipped elsewhere unless the image is configured.
- **Measurements:** `make parser-measure`.

## Extraction and evaluation (DI-1)

`src/api/extraction/` implements the `service_agreement.v1` contract (D-062 F1) with deterministic rules. There is no model, embedding or retrieval.
- **Input:** the parser sandbox's validated page text.
- **Output:** all 7 fields (`provider_name`, `customer_name`, `effective_date`, `end_date`, `governing_law`, `contract_value`, `notice_period_days`).
  - Each field is `found`, `unknown` (`not_found` / `unparseable` / `ambiguous`) or `conflict`, and every candidate is kept with exact evidence (`page`, `start`, `end`, `quote`).
  - The outcome is `needs_review` when a required field (provider, customer, effective date) is not found or any field is in conflict.
  - There is no numeric confidence, and a conflict never gets a winner.
- **Validation:** `contract.py` validates the result's structure and consistency. `evidence_violations` checks every quote against its page and every raw value against its quote.
- **Normalizers** (`normalize.py`) never guess:
  - a numeric date with two valid readings is `ambiguous`;
  - months or business days are not days;
  - `$` is not a currency;
  - only fixed date, amount and currency forms are accepted.
- **Rules** (`extract.py`): labelled lines (a label counts anywhere on a line, so a look-alike in prose surfaces as a conflict) and the clause forms the dev corpus uses.

**`make eval`** (`scripts/evaluate.py`, `scripts/eval_scoring.py`) runs every corpus case through the real parser sandbox, then extraction, then scoring against `corpus/expected/extract`. It stops if the parsed text drifts from `corpus/expected/parse` or the sandbox is unavailable. `--extractor NAME` chooses the extractor (only `rules` today). Gates:
- **Every split, hard:** schema-valid results, every quote resolving, zero unsupported claims, exact failure codes.
- **Dev, hard:** outcome, field and evidence accuracy, and abstention and conflict precision/recall, all at 1.0; no silent conflicts; no wrong values (accepted values the expected output does not list, such as a planted value quoted verbatim).
- **Each held-out split (`heldout`, `heldout-2`):** no regression against its `corpus/eval/<split>-baseline.json`. `make eval-baseline SPLIT=<split>` rewrites one baseline, only when every hard gate passes.

See `corpus/README.md` for the splits and the integrity rules. Outside the evaluation, the processing worker (below) is the extractor's only caller.

## Processing (DI-1): runs, worker, results

Migration 0008 stores processing runs and their results. `src/api/processing.py` is the members' side (start and read, inside `tenant_transaction`), and `src/api/worker.py` is the worker.
- **Pipeline version:** `service_agreement.v1+<extractor>+parser@<PARSER_IMAGE_ID>`. `PARSER_IMAGE_ID` is the parser image's full `sha256:` id.
  - The API needs it to start processing (503 `processing_unavailable` without it; a malformed value stops startup).
  - The worker refuses to start unless Docker has exactly that image, and runs it by id, so every run records the exact parser artifact.
  - `(version, pipeline version)` is unique, so starting is idempotent.
- **States:** `queued` -> `running` -> `completed` / `needs_review` / `failed`. The database allows only these transitions (and a retry, running -> queued), caps attempts at 3, and never changes a finished run. A finished run always has its pages and all seven fields (checked at commit).
- **Worker** (`make api-worker`, `ARGS=--once` for one run; `DATABASE_URL_WORKER` as `app_worker`):
  - It claims the oldest due run of its pipeline version (`FOR UPDATE SKIP LOCKED`) with a lease (`PROCESSING_LEASE_SECONDS`, default 300; there is no heartbeat, so the lease must outlast the sandbox's worst case, which is checked).
  - It reads the bytes through row-level security (only its claimed run's version, only while the latest verdict is clean) and checks them against the stored SHA-256.
  - It parses in the sandbox, extracts, and writes every result in the one transaction that ends the run, only while it still holds the lease.
  - Only `sandbox_unavailable` is retried (after `PROCESSING_RETRY_DELAYS`, default 30 and 120 seconds). A run whose lease expires is taken over as another attempt, and on its last attempt fails as `worker_lost`. Every other failure is final.
  - Failure codes: the parser's (`parse_failed`, `no_text_layer`, `embedded_content`, `timeout`, ...), `not_clean`, `integrity_mismatch`, `extraction_failed`, `sandbox_unavailable`, `worker_lost`.
- **Access (row-level security):**
  - Members read their tenant's runs and results. Page text, candidates and evidence quotes are readable only while the version's latest verdict is clean, like the bytes.
  - `app_worker` sees the queue of every tenant (ids and state only) and, for the run it holds, the bytes, hash and verdicts of that version; it inserts results only for that run. It cannot read users, memberships, names or audit rows, or read back what it wrote beyond the completeness check.
  - The operator has no access.
- **Logging:** run ids, statuses and failure codes only; never document text.
- **Tests:** `tests/test_processing_*.py` and `tests/test_worker.py` (fake sandbox), plus `tests/test_processing_e2e.py` through the real sandbox (mandatory in CI, dev corpus cases only). `TEST_DATABASE_URL_WORKER` is the disposable test database as `app_worker` (`scripts/local-postgres.sh test-db` writes it).
- **Development database:** migrated to 0008 on 30 Sep 2026 with approval; later migrations are applied there only with separate approval (`make api-db-migrate`). `scripts/local-postgres.sh worker-role` adds `app_worker` to an existing local cluster without granting it anything on that database.

## Review (R1): decisions, approvals, export

Migration 0009 adds the `reviewer` membership role and two append-only tables; `src/api/review.py` is the repository and the export model.
- **Who:** owners and reviewers record decisions and approve; members read the review state and download approved records. A reviewer is otherwise a member. Roles are granted only with the operator CLI (`--role owner|reviewer|member`).
- **Decisions** apply to the document's newest run once it `completed` or `needs_review`. A found or conflicting field can be resolved to one of its candidates that has a valid value (`candidate_ordinal` is its position in the `GET …/processing` result), keeping that candidate's evidence; any field can be resolved to UNKNOWN. There is no free text. Decisions are numbered per run (`seq`), and every write names the newest number the reviewer saw (`expected_seq`): anything else is 409 `stale` and writes nothing.
- **Blocking fields** are every conflict and every required field that is not found (the rows the run marks for review). Approval is refused with 422 `unresolved_fields` while one has no decision; the other fields are approved as extracted. The same person may decide and approve (no four-eyes approval yet).
- **Approvals** are immutable versions per document (`expected_version` = the number of approvals the reviewer saw). Each freezes the run and the decisions up to `expected_seq` into an `approved_record.v1` document built from the stored rows only, stored as canonical JSON (sorted keys, no whitespace, UTF-8) with its SHA-256. Every field is either `found` with a value and at least one evidence span (page, character range, exact quote) or `unknown` with a reason (`not_found`, `unparseable`, `ambiguous`, or `rejected_by_reviewer`), and records its basis (`reviewer_decision` with the decision, or `approved_as_extracted`) and the extracted state. The JSON Schema is committed as `schemas/approved_record.v1.schema.json` (regenerate with `uv run --locked python -m scripts.record_schema`; a test fails when it is out of date).
- **Export:** the stored bytes exactly, checked against the stored hash and the model before they are served (otherwise 503).
- **Database rules (also without the API):** row-level security lets only owners and reviewers insert, only as themselves; ids and timestamps are set by the database. Triggers check that a decision chooses a candidate with a value of a found or conflicting field of a finished run, that numbers are previous + 1 (a race loses with a unique violation), that an approval's `decisions_through` is the run's newest decision (SQLSTATE `RV001` otherwise), and that the record matches its row and hash and is structurally grounded: every `found` field has a value and at least one evidence span, every `unknown` field has no value and a reason. The database does not compare that evidence with the stored candidates: the API builds each record only from the stored run, candidates, evidence and decisions, so a value and its evidence are exactly the chosen or extracted candidate's (verified by the endpoint tests). Both tables refuse UPDATE, DELETE and TRUNCATE for every role, including `app_owner`. Approved records (they contain quotes) are readable only while the version's latest verdict is clean. `app_worker` and the operator have no access.
- **Tests:** `tests/test_review_endpoints.py` (API, concurrency), `tests/test_review_schema.py` (the database rules directly), `tests/test_review_record.py` (the record model and schema).

## Operator CLI

`src/api/operator.py` creates tenants and administers memberships; there is no HTTP administration. It connects with `DATABASE_URL_OPERATOR` and refuses every role except `app_operator` (which can read users, create tenants by name, create, re-role and delete memberships, and append to `operator_audit`; it has no access to workspaces, no DDL, and cannot change audit rows).

```bash
make api-admin ARGS='tenant create --name "Acme" --owner-user-id <user id> --request-id onboard-acme-1 --reason "pilot customer"'
make api-admin ARGS='membership grant --tenant-id <tenant id> --user-id <user id> --role member --reason "joined team"'
make api-admin ARGS='membership revoke --tenant-id <tenant id> --user-id <user id> --reason "left team"'
make api-admin ARGS='tenant show --tenant-id <tenant id>'
```

- Users are given by `--user-id` (from `GET /v1/me`) or by `--issuer` and `--subject` together (`--owner-…` for `tenant create`). The CLI never creates users: a user must have signed in once.
- Each command is one transaction; the change and its `operator_audit` row (time, database role, action, tenant, user, old and new role, request id, reason) commit together or not at all. Only changes are audited. Reasons are free text: do not put personal data in them.
- `tenant create` is idempotent per `--request-id`: repeating it with the same name and owner reports the existing tenant; reusing the id for anything else is refused. Concurrent duplicates create one tenant.
- `grant` and `revoke` are idempotent (no change → `unchanged`). The tenant's last owner can be neither revoked nor demoted; changes to one tenant's memberships are serialised (advisory transaction lock), so concurrent commands cannot remove the last owner.
- Output is one JSON object; exit codes: 0 done or unchanged, 2 usage, 3 refused (nothing changed), 4 database unavailable or misconfigured. Connection URLs and SQL errors are never printed.
- Every command waits at most 5 seconds for any lock (`lock_timeout`, e.g. while another operator command changes the same tenant). If that is exceeded it exits 4 with `"error": "busy"`; its transaction is rolled back, so nothing (including audit rows) is written and the command can simply be retried.
- The database must be migrated first (`make api-db-migrate`); before revision 0005, `app_operator` has no grants and every command exits 4.
- Limitation: `app_owner` (the migration role) can still modify `operator_audit`.

## Schema and migrations

`src/api/models.py` defines the tables; `migrations/versions/` holds hand-reviewed Alembic revisions that must match them (`alembic check` in `make api-check`). Migrations run only through `make api-db-migrate`, as `app_owner`; `migrations/env.py` refuses any role that does not own schema `app` or is a superuser. Row-level security policies and runtime grants are part of the migrations: `app_runtime` may read only the tenant set for the current transaction, and read and insert that tenant's `workspaces` only if the transaction's user (`app.user_id`) is a member of it; it cannot update or delete workspaces (not granted until endpoints need it). `app_runtime` reads a tenant row only for the transaction's tenant and only if its user is a member. `app_owner` administers everything; tenants and memberships are otherwise changed only by `app_operator` (see Operator CLI).

`users` holds only an identity key, (`issuer`, `subject`) of at most 512 and 255 characters, with no email or name. `memberships` links a user to a tenant as `owner`, `reviewer` or `member`. Both have forced row-level security: `app_runtime` may insert (issuer and subject columns only) and read the user matching the transaction-local `app.auth_issuer`/`app.auth_subject`, read its own user by `app.user_id`, and read only that user's memberships; it can never update or delete users or write memberships, which are managed by the operator CLI (`app_operator`). `identity.py` and `tenant_transaction` set these values. These settings, like `app.tenant_id`, protect against application mistakes, not against arbitrary SQL executed as `app_runtime`, which could set them itself.
