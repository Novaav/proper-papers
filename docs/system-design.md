# System design

As built, not a plan. Status: local development and CI only; nothing is deployed. Controls and tests are in [`security.md`](security.md), measurements in [`evaluation.md`](evaluation.md).

## Components

```
Browser ──HTTPS──> Next.js server (apps/web)          sign-in, pages, route handlers
                        │  access token in a sealed cookie, read server-side only
                        ▼
                   FastAPI (apps/api)  ──────────────> ClamAV (clamd), scan before storage
                        │  runtime role, row-level security
                        ▼
                   PostgreSQL 18  <──── worker (apps/api, `api.worker`): claims runs with FOR UPDATE SKIP LOCKED
                                              │
                                              └─ one container per document (apps/parser): no network, read-only
```

| Part | Responsibility |
|---|---|
| `apps/web` | Next.js (App Router, TypeScript). Server components and route handlers call the API; the browser never does. OpenID Connect sign-in (Authorization Code with PKCE). Plain CSS, no UI framework. |
| `apps/api` | FastAPI, SQLAlchemy, Alembic. Token verification, tenant authorization, workspaces, documents, processing runs, review, approval and export. |
| Worker | The same package, run as its own process and database role. Takes queued runs, runs the parser sandbox and the rules extractor, stores the result. |
| `apps/parser` | pdfminer.six in a locked-down container, one document per container. Produces page text and nothing else. |
| `corpus/` | The synthetic documents, ground truth and evaluation reports. |

Extraction is deterministic rules (`apps/api/src/api/extraction/extract.py`). A model-assisted variant exists only as evaluation tooling and is not used at runtime (see [`evaluation.md`](evaluation.md)).

## A document's path

1. **Upload** (PDF only, size-capped, encrypted PDFs refused). The API scans the bytes with ClamAV and stores them in PostgreSQL only on a clean verdict; the hash is recorded and the bytes are returned unchanged on download.
2. **Process** is an explicit action. It inserts a `queued` run. A document with no run is "not processed", which is different from queued.
3. **Worker** claims the run, parses the PDF in the sandbox, extracts the `service_agreement.v1` record (7 fields, 3 required), checks that every value's evidence resolves to the page text, and stores the pages, candidates and evidence. The run ends `completed`, `needs_review` or `failed`. A failed run is never re-queued; the recovery is to upload a corrected copy.
4. **Review.** Blocking fields (every conflict, every required field not found) need a decision before approval; a reviewer may decide any other field too. A decision picks one extracted candidate with a valid value, or UNKNOWN. No free text. Decisions are appended and numbered; other fields are approved as extracted and marked so.
5. **Approve.** Approval freezes the run and the decisions up to a number into `approved_record.v1` JSON with its SHA-256, as a new immutable version. A later decision makes the record "changed since version N"; approving again creates version N+1.
6. **Export** returns the stored bytes of one version after re-checking its hash and schema.

## Data

PostgreSQL schema `app`: `tenants`, `users`, `memberships`, `workspaces`, `documents`, `document_versions`, `document_contents`, `document_scans`, `processing_runs`, `document_pages`, `extracted_fields`, `field_candidates`, `candidate_evidence`, `review_decisions`, `approved_records`, `operator_audit`. Migrations `0001`–`0009` in `apps/api/migrations/versions`; the API never migrates on startup.

## Invariants and where they are enforced

| Invariant | Enforced by |
|---|---|
| A request sees only its own tenant's rows, and only as a member | Forced row-level security and the membership check in `tenant_transaction` |
| No document version exists without a clean first scan; content is readable only while the latest verdict is clean | Deferred constraint trigger and row-level security (migration `0007`) |
| Stored document bytes are the uploaded bytes | SHA-256 recorded at upload; the same bytes are returned |
| Every found value has evidence that equals the page text at its offsets | `evidence_violations` in the worker, and structure checks in the database |
| A value is never invented: missing evidence is UNKNOWN with a reason, disagreement is a conflict | The extraction contract (`extraction/contract.py`); no numeric confidence exists |
| Approved versions never change | Update, delete and truncate triggers; the runtime role can only read and insert (an insert needs a reviewer, checked by row-level security), never update or delete |
| Status shown is stored state | The page reads the run's status from the API; polling only re-reads it |

## Decisions and trade-offs

| Decision | Why | Trade-off or residual risk |
|---|---|---|
| Postgres job table with `FOR UPDATE SKIP LOCKED`, one worker, no broker | Runs stay durable and tenant-scoped in the database that already holds everything | No broker features (priorities, back-off, fan-out); throughput is bounded by Postgres and the number of workers |
| One container per document, no in-process fallback | A hostile PDF never runs in the API or worker process; if the sandbox cannot run, the result is `sandbox_unavailable`, not an unsandboxed parse | Needs Docker; a deployed worker must not hold the Docker socket, so a production runner is a release blocker |
| Rules extractor at runtime; model assistance evaluated and not enabled; a model may not change a field the rules found anything for | The rules produced no wrong values on any measured split; the assisted variants failed pre-registered gates ([`evaluation.md`](evaluation.md)) | Lower accuracy on unseen phrasings |
| Forced row-level security plus a membership check; the API's role cannot bypass it and the engine refuses roles that could | Isolation holds even if application code forgets a filter | Policies key on session settings, which arbitrary SQL run as the runtime role could set; mitigated, not removed ([known gaps](security.md#known-gaps)) |
| Scan before storing, fail closed; a version cannot exist without a clean first scan | An unscanned or unscannable file is never stored or served | A scanner outage stops uploads (reads and downloads continue); ClamAV's blind spots are listed in the [known gaps](security.md#known-gaps) |
| Document bytes in PostgreSQL (`bytea`), same transaction and policy as the metadata | One consistency and isolation model | Database growth and memory per request; object storage deferred |
| Stateless sealed session cookie holding only the access token, one-hour cap, no refresh token | No session store to run; the token is never exposed to page scripts or sent by the browser to the API | A stolen cookie works until it expires ([known gaps](security.md#known-gaps)) |
| Review decisions pick an extracted candidate or UNKNOWN, no free text; decisions append-only; approvals immutable versions | Every approved value keeps provenance to extracted text | A value the extractor missed stays UNKNOWN; reprocessing would start a new review |
| A `reviewer` role; members are read-only | Least privilege for approval without granting the administrative role | No four-eyes approval: the same person can decide and approve |

## Not decided or not present

Hosting provider, object storage, OCR (scanned PDFs are unsupported), vector search or retrieval (nothing retrieves), caching, rate limiting, and a production runner for the parser that does not need the Docker socket (see [`deployment-security.md`](deployment-security.md)).

A live demo is a later goal and is not planned in detail. A hosted front end alone would not be enough: the API, PostgreSQL, worker and malware scanner each need hosting, and the parser needs an isolated container runner. Hosting choices and costs are undecided.
