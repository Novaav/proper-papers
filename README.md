# Proper Papers

**Every claim has a source.** Evidence-grounded document intelligence.

Proper Papers turns service-agreement PDFs into structured records with verifiable source evidence. Missing information stays unknown, conflicting information requires human review, and approved records can be exported as versioned JSON. In the review workbench, every value sits beside the exact text it came from.

This is a portfolio project, but a working full stack, not a mock-up: a Next.js review workbench over a FastAPI service, PostgreSQL with row-level security (the database itself filters rows per tenant), a worker that parses each PDF in an isolated container, and ClamAV scanning before anything is stored. The scope is deliberately narrow (synthetic service agreements, seven fields) so the behaviour can be measured. **Status:** extraction at runtime is deterministic rules only; the workflow below is implemented and tested; model-assisted extraction was evaluated and is not enabled; nothing is deployed and it is not production-ready (see [Status and limits](#status-and-limits)).

Where to look first:

- **The workflow** ([below](#the-workflow)): upload, scan, extract, review, approve, export, with the code behind each step.
- **Evaluation** ([below](#evaluation)): a model-assisted extractor was measured against thresholds fixed in advance. It raised accuracy substantially and failed the reliability gates, so the runtime stays rules-only.
- **Isolation** ([below](#security-and-tenant-isolation)): tenant boundaries are enforced in the database, with the controls, tests and known gaps in one place.
- **Verification** ([below](#verification)): what CI proves, what was checked by hand, and what was not checked.

## The problem

Language-model demos tend to return fluent answers that are hard to check: no source, no sign of what is missing, no sign that two clauses disagree. The aim here was a system where every value can be traced to its source, missing information is stated, conflicts are shown, and a person's decision is on record.

## The workflow

| Step | What happens | Where |
|---|---|---|
| Upload | PDF only, size-capped, encrypted PDFs refused. Scanned with ClamAV before anything is stored; a scanner failure refuses the upload. | `apps/api/src/api/documents.py`, `scanning.py` |
| Process | An explicit action. The run is queued in PostgreSQL; a worker parses the PDF in a short-lived container with no network and extracts the record with deterministic rules. | `worker.py`, `sandbox.py`, `apps/parser/`, `extraction/extract.py` |
| Review | Conflicts and missing required fields need a decision: pick one extracted candidate, or UNKNOWN. No free-text edits. | `review.py`, web document page |
| Approve | Freezes the run and the decisions into a new immutable version with a SHA-256. A later decision means "changed since version N"; approving again makes version N+1. | `review.py`, migration `0009` |
| Export | Returns the stored JSON of one version after re-checking its hash. | `review.py` |

A field is one of: found (value plus evidence), unknown (with a reason: `not_found`, `unparseable` or `ambiguous`), or conflict (every candidate with its own quote, none preferred). There are no confidence scores. Below is an abridged real approved record for a synthetic document that states two effective dates (`dev-c1-conflict-date`): the extractor reported a conflict, the reviewer chose the April reading, and the record keeps both facts.

```json
"effective_date": {
  "status": "found", "value": "2026-04-01", "basis": "reviewer_decision",
  "extracted": { "status": "conflict" },
  "decision": { "seq": 1, "decided_at": "2026-10-09T14:03:57+02:00" },
  "evidence": [{ "page": 1, "start": 147, "end": 178, "quote": "is effective as of 1 April 2026" }]
}
```

The same document has four fields it cannot support; they were approved as explicit unknowns, not guesses.

## Architecture

```
Browser → Next.js server → FastAPI → PostgreSQL 18 (row-level security) ← worker → parser container
                              └→ ClamAV
```

Python 3.14 with FastAPI, SQLAlchemy and Alembic; Next.js (App Router, TypeScript); PostgreSQL 18; pdfminer.six in the parser. The choices that cost something, each with its trade-off in [`docs/system-design.md`](docs/system-design.md):

- A Postgres job table with `FOR UPDATE SKIP LOCKED` and one worker, instead of a broker.
- One throwaway container per PDF with no in-process fallback, which makes a production runner without the Docker socket a release blocker.
- A rules extractor at runtime, because the model-assisted one failed its pre-registered gates (below).
- Forced row-level security plus a role the engine refuses to use if it could bypass it, accepting that session-setting-based policies don't stop arbitrary SQL run as that role.

## Security and tenant isolation

Tenants are isolated by PostgreSQL row-level security with forced policies: a request sees a row only if its tenant matches and its user is a member. Tenants are created only by an operator CLI, uploads are scanned before storage, the access token stays server-side, and approved records cannot be updated or deleted, even by the runtime role. A review before merge found that a role belonging to the owner role bypassed the first version of the runtime-role guard and could read every tenant; the guard now covers role membership, and the regression tests fail against the old guard.

[`docs/security.md`](docs/security.md) lists each control with the code that enforces it and the tests that exercise it, and the known gaps. Tests show the controls work in the scenarios written for them; there has been no external audit or penetration test.

## Evaluation

The corpus is 80 synthetic PDFs with ground truth: dev (22; the rules were written against it), three held-out splits (8, 16 and 24) and a 10-case development split for the model-assisted work. The held-out sets and the pass/fail thresholds were committed before the extractor they judge was frozen. Full method and tables: [`docs/evaluation.md`](docs/evaluation.md).

The rules extractor is exact on dev and conservative on unseen phrasings. On held-out data its field accuracy is 0.63–0.77 with no wrong values and abstention recall 1.0: it misses cases, but it produced no wrong values on these sets.

A model-assisted extractor was built and measured twice (the model proposes quoted spans for fields the rules left empty; every span is checked against the page text; the model cannot change a field the rules found). Worst of three runs against the rules on the same held-out split:

| Held-out split | Field accuracy (rules → assisted) | Evidence completeness | Wrong values | Silent conflicts |
|---|---|---|---|---|
| `heldout-2`, 16 cases (assisted-1) | 0.6339 → 0.9643 | 0.3538 → 0.9385 | 0 → **2** | 1 → 1 |
| `heldout-3`, 24 cases (assisted-2) | 0.7679 → 0.9762 | 0.4459 → 0.9459 | 0 → 0 | 2 → **3** |

Assistance raised accuracy and evidence completeness substantially and still failed a gate fixed in advance each time (wrong values for assisted-1, silent conflicts for assisted-2), so the product stays rules-only. Nothing was tuned on held-out data and there was no third attempt. Committed cost data: mean USD 0.0058 per call, p95 latency 2.8–3.6 s. Every model response is committed, and CI replays them and must reproduce the reports (`make eval-assisted`, no API key, no model call).

Limits: one author wrote the held-out documents and the extractors; the sets are small and the generated text is more regular than real contracts. Read the numbers as directional.

## Verification

**Automated.** CI (GitHub Actions) runs on every pull request and on `main`: the API tests against real PostgreSQL, a real ClamAV engine and the real parser container; the parser tests; the web unit tests plus a runtime check against the production build; `make eval` (dev gates hard, held-out against committed baselines); and the offline replay of both model evaluations. Results at the last merge in the original development repository (all four jobs green): API 1319 passed (none skipped), parser 132, web 232 unit tests and 260 runtime assertions. This repository's own Actions tab shows its CI results.

**Manual, local, 9 Oct 2026.** The full workflow was walked through in a browser against the real API, worker, parser, ClamAV and a disposable PostgreSQL database: upload and scan; "not processed" versus a genuinely queued run; live status to completion; extraction against ground truth (7/7 values and evidence); conflict review; decisions surviving a reload; approval versions 1 and 2; an export whose SHA-256 matched the stored record; invalid uploads and other tenants' URLs refused; all data intact after restarting the API, worker and web app (PostgreSQL stayed up). Database state was checked after each stage. This is evidence from one session, not part of CI.

The review-flow and theme polish made after that session (auto-advance to the next pending field, decision colours, light/dark toggle) is covered by unit and runtime checks and was reviewed by the author in a browser; the full walk-through above was not repeated.

**Not verified:** Safari, Firefox and screen-reader behaviour of the newest controls; cross-tenant access attempted by a second signed-in user in a browser (isolation is tested at database and API level); load testing; anything deployed.

## Status and limits

- Not deployed. A live demo is a later goal and hosting is undecided; the front end alone would not be enough, since the API, database, worker and scanner need hosting too. Required before any public deployment ([`docs/deployment-security.md`](docs/deployment-security.md)): rate limiting, HSTS, a parser runner without the Docker socket, and scanner operations.
- One document type, seven fields, text PDFs only (no OCR). The extraction rules are specific to this domain.
- No four-eyes approval: the same person may decide and approve a record.
- Known security gaps are listed in [`docs/security.md`](docs/security.md#known-gaps).
- Licensed under the MIT License ([`LICENSE`](LICENSE)); third-party notices are in [`NOTICE`](NOTICE).

## Inspect it

- Evidence handling: `apps/api/src/api/extraction/contract.py` (`evidence_violations`), then `apps/api/tests/test_extraction.py`.
- Isolation: `apps/api/migrations/versions/0002_tenants_rls.py`, `0004_workspace_membership_rls.py`, `apps/api/src/api/database.py`, `apps/api/tests/test_rls_isolation.py`.
- Sandbox: `apps/api/src/api/sandbox.py`, `apps/api/tests/test_parser_sandbox.py`.
- Review and immutability: `apps/api/src/api/review.py`, `apps/api/migrations/versions/0009_review.py`, `apps/api/tests/test_review_schema.py`.
- Evaluation: [`docs/evaluation.md`](docs/evaluation.md), `corpus/eval/`, `apps/api/scripts/eval_scoring.py`.

## Run it

`make`, `uv`, Node 24 and Docker are required.

```sh
make api-setup web-setup parser-setup   # locked dependencies
make api-db-install api-db-up           # a disposable local PostgreSQL 18 (builds from source, once)
make check                              # parser, API and web checks; sandbox and real-scanner tests need Docker, the parser image and ClamAV, and are skipped without them (CI runs them all)
make eval                               # extraction evaluation through the real sandbox
```

[`docs/demo.md`](docs/demo.md) walks through the whole workflow locally on a disposable database with four synthetic documents; it needs a development OpenID Connect application (Auth0 was used). Everything under `.local/` is gitignored. The test and demo commands only touch databases carrying a disposable marker; the development database is separate. See [`apps/api/README.md`](apps/api/README.md), [`apps/web/README.md`](apps/web/README.md) and [`apps/parser/README.md`](apps/parser/README.md) for component details.

## How this was built

I set the scope, made the decisions recorded here, and reviewed and approved every merge. Most of the code was written with an AI coding assistant (Claude Code), and architecture and diffs were also reviewed with ChatGPT. This is separate from the model evaluated above, which is not used at runtime.

## Licence

MIT, copyright (c) 2026 Nova Zandkarimi. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
