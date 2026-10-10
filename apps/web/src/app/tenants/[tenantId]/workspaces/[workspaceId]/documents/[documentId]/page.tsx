import { notFound, redirect } from "next/navigation.js";

import { getDocument, getWorkspace } from "@/lib/api/documents.ts";
import { getProcessing, type ProcessingRun } from "@/lib/api/processing.ts";
import { getReview } from "@/lib/api/review.ts";
import { reauthenticateOnUnauthorized } from "@/lib/auth/redirect.ts";
import { loginPath, nowSeconds, readSession } from "@/lib/auth/server.ts";
import { formatSize, formatTime } from "@/lib/format.ts";
import { isCanonicalUuid } from "@/lib/ids.ts";
import {
  recordView,
  statusView,
  type StatusView,
} from "@/lib/processing/view.ts";
import { reviewView } from "@/lib/review/view.ts";

import { Toolbar } from "../../../../../../toolbar.tsx";
import { PageNotice } from "./page-notice.tsx";
import { ProcessingStatus } from "./processing-status.tsx";
import { Record } from "./record.tsx";

export const dynamic = "force-dynamic";

const SCAN_LABELS = {
  clean: "Clean",
  not_scanned: "Not scanned",
  blocked: "Blocked",
} as const;
const SCAN_BADGES = {
  clean: "badge badge--success",
  not_scanned: "badge badge--attention",
  blocked: "badge badge--failed",
} as const;

// Fixed messages for the codes the process route redirects with; any other
// value is ignored, so nothing from the URL is ever rendered.
const STARTED: Record<string, string> = { started: "Processing started." };
const PROCESS_ERRORS: Record<string, string> = {
  processing_unavailable:
    "Processing isn't available right now. Try again later.",
  unavailable: "Processing could not be started right now. Try again.",
  not_scanned:
    "Processing is only available for documents that passed the malware scan.",
  blocked:
    "Processing is only available for documents that passed the malware scan.",
  rejected:
    "The API rejected this sign-in. Sign out and try again from your account page.",
};

// Fixed messages for the review routes' codes (?review=…); others are ignored.
const REVIEW_DONE: Record<string, string> = {
  decided: "Decision saved.",
  approved: "Record approved.",
};
const REVIEW_ERRORS: Record<string, string> = {
  stale:
    "This record changed since you opened it. Nothing was saved; review the current state.",
  run_changed:
    "The processing result changed. Nothing was saved; review the current result.",
  invalid: "That decision is not possible for this field. Nothing was saved.",
  unresolved: "Decide every field marked Needs review before approving.",
  not_reviewer: "Only reviewers can review and approve.",
  not_clean:
    "Review is only available for documents that passed the malware scan.",
  rejected: PROCESS_ERRORS.rejected ?? "",
  unavailable: "The review could not be saved right now. Try again.",
};

interface Props {
  params: Promise<{
    tenantId: string;
    workspaceId: string;
    documentId: string;
  }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

// Server Component: reads the session, never changes cookies. The API decides
// membership, the document and its malware verdict on every call; this page
// only renders its answers. Processing results are rendered here, on the
// server; the browser only ever asks for the processing status (processing-status.tsx).
export default async function DocumentPage({ params, searchParams }: Props) {
  const { tenantId, workspaceId, documentId } = await params;
  if (![tenantId, workspaceId, documentId].every(isCanonicalUuid)) notFound();
  const workspacePath = `/tenants/${tenantId}/workspaces/${workspaceId}`;
  const path = `${workspacePath}/documents/${documentId}`;

  const session = await readSession();
  if (!session) redirect(loginPath(path));

  const base = process.env.API_BASE_URL;
  const token = session.accessToken;
  const [workspace, document, processing, review] = await Promise.all([
    getWorkspace(base, token, tenantId, workspaceId),
    getDocument(base, token, tenantId, workspaceId, documentId),
    getProcessing(base, token, tenantId, workspaceId, documentId),
    getReview(base, token, tenantId, workspaceId, documentId),
  ]);
  const answers = [workspace, document, processing, review];
  if ([workspace, document, processing].some((a) => a.state === "not_found")) {
    notFound();
  }
  const rejected = answers.some((answer) => answer.state === "unauthorized");
  if (rejected && reauthenticateOnUnauthorized(session, nowSeconds())) {
    redirect(loginPath(path));
  }

  const query = await searchParams;
  const flash =
    lookup(STARTED, query.processing) ?? lookup(REVIEW_DONE, query.review);
  const error = rejected
    ? PROCESS_ERRORS.rejected
    : (lookup(PROCESS_ERRORS, query.processing) ??
      lookup(REVIEW_ERRORS, query.review));
  const workspaceName =
    workspace.state === "ok" ? workspace.workspace.name : "Workspace";

  if (document.state !== "ok") {
    return (
      <main id="main" tabIndex={-1} className="document page">
        <Toolbar
          crumbs={[
            { label: "Workspaces", href: `/tenants/${tenantId}/workspaces` },
            { label: workspaceName, href: workspacePath },
            { label: "Document" },
          ]}
        />
        <div className="page-body">
          <h1>Document</h1>
          <p role="alert">
            {error ?? (
              <>
                The document is unavailable right now.{" "}
                <a href={path}>Try again</a>.
              </>
            )}
          </p>
        </div>
      </main>
    );
  }

  const { document: doc } = document;
  const run = processing.state === "ok" ? processing.run : null;
  const record =
    processing.state === "ok" && processing.result
      ? recordView(processing.result)
      : null;
  const reviewing =
    record && run && review.state === "ok"
      ? reviewView(record, run.id, review.review)
      : null;
  const status = pipelineStatus(processing.state, run, record, reviewing);
  // The success message says what the API now says, not just that a form was
  // sent: the remaining review count and the approved version come from the
  // review state read for this very page load.
  const started =
    query.review === "decided" && flash
      ? `${flash}${remainingText(record, reviewing)}`
      : query.review === "approved" && flash
        ? approvedText(flash, reviewing)
        : flash;

  return (
    <main id="main" tabIndex={-1} className="document page">
      <Toolbar
        crumbs={[
          { label: "Workspaces", href: `/tenants/${tenantId}/workspaces` },
          { label: workspaceName, href: workspacePath },
          { label: doc.name },
        ]}
      >
        {doc.scanStatus === "clean" && (
          <a className="button-secondary" href={`${path}/download`}>
            Download PDF
          </a>
        )}
      </Toolbar>
      <div className="page-body">
        {started && <PageNotice role="status" message={started} />}
        {error && <PageNotice role="alert" message={error} />}
        <header className="document-head">
          <h1>{doc.name}</h1>
          <p className="document-meta">
            {formatSize(doc.sizeBytes)} · Uploaded{" "}
            <time dateTime={doc.createdAt}>{formatTime(doc.createdAt)}</time> ·
            SHA-256 <code title={doc.sha256}>{doc.sha256.slice(0, 12)}…</code>
          </p>
        </header>

        <section className="pipeline" aria-labelledby="pipeline-heading">
          <h2 id="pipeline-heading" className="visually-hidden">
            Status
          </h2>
          <dl className="pipeline-facts">
            <div>
              <dt>Malware scan</dt>
              <dd>
                <span className={SCAN_BADGES[doc.scanStatus]}>
                  {SCAN_LABELS[doc.scanStatus]}
                </span>
              </dd>
            </div>
            <div>
              <dt>Processing</dt>
              <ProcessingStatus
                statusUrl={`${path}/processing`}
                live={
                  run && (run.status === "queued" || run.status === "running")
                    ? run.status
                    : null
                }
                tone={status.tone}
                text={status.text}
              />
            </div>
            {reviewing && (
              <div>
                <dt>Approval</dt>
                <dd>
                  <span className={approvalBadge(reviewing).className}>
                    {approvalBadge(reviewing).text}
                  </span>
                </dd>
              </div>
            )}
          </dl>
          {(processing.state === "unavailable" || status.live) && (
            <p className="pipeline-check">
              <a href={path}>Check status</a>
            </p>
          )}
        </section>

        {status.live && (
          <section className="next-step" aria-labelledby="waiting-title">
            <div>
              <h2 id="waiting-title">Nothing to do while this runs</h2>
              <p>
                A worker takes the document, reads the PDF in an isolated
                sandbox and extracts its fields. This page updates when the run
                changes, and the status is kept if you leave.
              </p>
            </div>
          </section>
        )}

        {run?.status === "failed" && (
          <section className="next-step" aria-labelledby="failed-title">
            <div>
              <h2 id="failed-title">
                This document can&apos;t be processed again
              </h2>
              <p>
                A failed run is not repeated: the same document gives the same
                result. Upload a corrected copy of the PDF to the workspace to
                try again.
              </p>
            </div>
            <a className="button-secondary" href={`${workspacePath}#upload`}>
              Upload a corrected copy
            </a>
          </section>
        )}

        {processing.state === "ok" && run === null && (
          <section className="next-step" aria-labelledby="next-step-title">
            <div>
              <h2 id="next-step-title">Next: process this document</h2>
              <p>
                The PDF is read in an isolated sandbox and a service-agreement
                record is extracted, each value with the text it came from.
              </p>
            </div>
            <form id="process-form" method="post" action={`${path}/process`}>
              <button type="submit" className="button-primary">
                Process document
              </button>
            </form>
          </section>
        )}

        {record && (
          <Record
            record={record}
            review={reviewing}
            reviewUnavailable={review.state === "unavailable"}
            path={path}
            source={doc.name}
          />
        )}
        {run && <About run={run} sha256={doc.sha256} />}
      </div>
    </main>
  );
}

// After a decision: how many flagged fields are left, from the review state
// the API returned for this page. Nothing when the record has no flagged field.
function remainingText(
  record: ReturnType<typeof recordView> | null,
  review: ReturnType<typeof reviewView> | null,
): string {
  if (!record || !review || record.attention.length === 0) return "";
  const left = review.unresolved.length;
  if (left > 0)
    return ` ${left} ${left === 1 ? "field" : "fields"} left to review.`;
  return review.canApprove
    ? " Every flagged field is decided; the record can be approved."
    : " Every flagged field is decided.";
}

// After an approval: the version the API now lists as current, if it does.
function approvedText(
  fallback: string,
  review: ReturnType<typeof reviewView> | null,
): string {
  const newest = review?.versions[0];
  return newest?.current
    ? `Record approved as version ${newest.version}.`
    : fallback;
}

// The record's approval state as a fact in the status strip.
function approvalBadge(review: NonNullable<ReturnType<typeof reviewView>>): {
  className: string;
  text: string;
} {
  const newest = review.versions[0];
  if (!newest) return { className: "badge", text: "Not approved" };
  return newest.current
    ? {
        className: "badge badge--success",
        text: `Approved, version ${newest.version}`,
      }
    : {
        className: "badge badge--attention",
        text: `Changed since version ${newest.version}`,
      };
}

function pipelineStatus(
  state: string,
  run: ProcessingRun | null,
  record: ReturnType<typeof recordView> | null,
  reviewing: ReturnType<typeof reviewView> | null,
): StatusView {
  switch (state) {
    case "ok":
      // The run keeps its "needs review" status for good; once a reviewer has
      // decided every flagged field, saying "Needs review — 2 fields" next to
      // an approved record would contradict the page. The record's own band
      // and the Approval fact carry the rest.
      if (
        run?.status === "needs_review" &&
        reviewing &&
        reviewing.unresolved.length === 0
      ) {
        return {
          tone: "neutral",
          text: "Reviewed — every flagged field is decided.",
          live: false,
        };
      }
      return statusView(run, record);
    case "not_scanned":
    case "blocked":
      return {
        tone: "neutral",
        text: "Only available for documents that passed the malware scan.",
        live: false,
      };
    default:
      return {
        tone: "neutral",
        text: "The processing status is unavailable right now.",
        live: false,
      };
  }
}

function About({ run, sha256 }: { run: ProcessingRun; sha256: string }) {
  const [, extractor, parser = ""] = run.pipelineVersion.split("+");
  const image = parser.replace(/^parser@/, "");
  return (
    <details className="about">
      <summary>About this result</summary>
      <dl>
        {run.finishedAt && (
          <div>
            <dt>Finished</dt>
            <dd>
              <time dateTime={run.finishedAt}>
                {formatTime(run.finishedAt)}
              </time>
            </dd>
          </div>
        )}
        <div>
          <dt>Document SHA-256</dt>
          <dd>
            <code className="full-hash">{sha256}</code>
          </dd>
        </div>
        <div>
          <dt>Extractor</dt>
          <dd>{extractor ?? "unknown"}</dd>
        </div>
        <div>
          <dt>Parser image</dt>
          <dd>
            <code title={image}>{image.slice(0, 19)}…</code>
          </dd>
        </div>
        <div>
          <dt>Attempts</dt>
          <dd>{run.attempts}</dd>
        </div>
      </dl>
    </details>
  );
}

function lookup(
  messages: Record<string, string>,
  value: string | string[] | undefined,
): string | undefined {
  return typeof value === "string" && Object.hasOwn(messages, value)
    ? messages[value]
    : undefined;
}
