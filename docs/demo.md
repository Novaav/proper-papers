# Local demo

A walkthrough of the whole workflow on your own machine: upload a synthetic contract, process it, review what needs a person's decision, approve, and export the approved record. Everything runs locally against a **disposable demo database** and the project's synthetic corpus. Nothing is deployed, and the project is not production-ready (see [`deployment-security.md`](deployment-security.md)).

Processing is deterministic and rule-based; no AI model is involved. The extractor does not fill a field it cannot support: a value without evidence in the document's text is shown as unknown, disagreeing values are shown as a conflict, and a person decides.

## What you need

- The development setup from the [README](../README.md#run-it): `make api-setup web-setup parser-setup`, the local PostgreSQL cluster (`make api-db-install`, once), and Docker (for the malware scanner and the parser sandbox).
- A development OpenID Connect application (Auth0 in development) whose settings include the callback `https://localhost:3000/auth/callback` (Allowed Callback URLs) and `https://localhost:3000/` (Allowed Logout URLs).
- Two gitignored settings files (never commit them; values are your own):
  - `.local/api.env`: `AUTH_ISSUER`, `AUTH_AUDIENCE`, `AUTH_ALLOWED_CLIENT_IDS`, `CLAMD_ADDRESS=tcp://127.0.0.1:3310` and `PARSER_IMAGE_ID` (step 1). Database URLs are not needed here: the demo targets use the demo database's own.
  - `apps/web/.env.local`: `API_BASE_URL=http://127.0.0.1:8000`, `APP_ORIGIN=https://localhost:3000` and the `OIDC_*` and `SESSION_SECRET` settings (see `apps/web/.env.example`).

## The demo database

`app_demo` lives in the local cluster next to the development database `app`, which the demo never uses. It is marked with the database comment `disposable-demo-database`, and its connection URLs are in `.local/postgres/demo.env`.

| Command | Effect |
| --- | --- |
| `make demo-db` | Create `app_demo` if it is missing (an existing one is kept) and migrate it. |
| `make demo-reset` | Drop and recreate it: all demo data is lost. |
| `make demo-drop` | Drop it and remove `demo.env`. |

Every demo target first checks that `app_demo` exists with its marker and that every URL in `demo.env` names `app_demo`; otherwise it stops and connects to nothing. A database called `app_demo` without the marker is never used or dropped.

## 1. Start the stack

```sh
make api-db-up        # the local PostgreSQL cluster
make demo-db          # the disposable demo database, migrated
make api-scanner-up   # ClamAV on 127.0.0.1:3310 (first start downloads signatures)
make parser-image     # the parser sandbox image
docker image inspect --format '{{.Id}}' production-ai-flagship-parser:local   # -> PARSER_IMAGE_ID in .local/api.env
```

Then, each in its own terminal:

```sh
make demo-api         # the API on http://127.0.0.1:8000, as the runtime role on app_demo
make demo-worker      # the processing worker, as app_worker on app_demo
make web-dev-https    # the web app on https://localhost:3000 (self-signed certificate; the browser warns once)
```

## 2. Sign in and get a tenant

1. Open https://localhost:3000 and sign in. The account page shows your user ID and no tenants yet.
2. Tenants are created only by an operator, never over HTTP. Make yourself the owner of a demo tenant:

   ```sh
   make demo-admin ARGS='tenant create --name "Demo" --owner-user-id <your user id> --request-id demo-1 --reason "local demo"'
   ```

3. Reload the account page, open the tenant's workspaces and create a workspace.

Owners and reviewers can review and approve; a member sees the same pages read-only.

## 3. Upload synthetic documents

```sh
make demo-corpus      # writes four synthetic PDFs to .local/demo-corpus (DEMO_CORPUS_DIR=... to choose another folder)
```

The PDFs are generated from `corpus/` and checked against the committed manifest's SHA-256 first. They contain no real or customer data.

| File | What it shows |
| --- | --- |
| `dev-n1-labelled.pdf` | All seven fields found, each with its evidence. |
| `dev-c1-conflict-date.pdf` | Two different effective dates: a conflict a person must resolve. |
| `dev-m2-required-missing.pdf` | No effective date anywhere: a required field not found. |
| `dev-u1-unparseable.pdf` | An effective date label without a date: unparseable, so unknown. |

On the workspace page, choose a file and upload it. Every upload is scanned for malware before anything is stored; only a clean document is stored and offered for download.

## 4. Process

Open the document and choose **Process document**. The status updates while the worker parses the PDF in its isolated sandbox and extracts the record; the page refreshes when the run finishes.

- **Completed:** every field was found with evidence.
- **Needs review:** at least one field needs a person (a conflict, or a required field that was not found). The summary at the top links to each one.

Open a field to see the page and the exact quote its value comes from.

## 5. Review and approve (a person decides)

Human review is required for every field marked **Needs review**; the system never resolves these itself.

- **Conflict:** each candidate is shown with its own quote. Choose **Use this value** under one, or **Neither — mark unknown**.
- **Required field not found:** **Confirm unknown**.
- **Found field:** optionally **Confirm** or **Mark unknown**.

When nothing is left to decide, the approval panel offers **Approve record**. Approval creates an immutable version. If the record changed since you opened the page (for example, a decision in another tab), nothing is saved and the page says so.

## 6. Export

After approval, **Download JSON** returns the approved record (`approved_record.v1`) exactly as stored, with its SHA-256. Every field is either found, with its value and evidence, or unknown, with a reason, and says whether a reviewer decided it. A later decision shows "Changed since version N was approved" and offers a new version; earlier versions stay unchanged.

## Stop and clean up

```sh
# Ctrl-C in the API, worker and web terminals, then:
make api-scanner-down
make demo-reset       # optional: start the next demo from an empty database (or make demo-drop)
make api-db-down
```

Remove the temporary callback and logout URLs from the OpenID Connect application if you added them only for the demo.
