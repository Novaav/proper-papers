import { notFound, redirect } from "next/navigation.js";

import {
  getWorkspace,
  listDocuments,
  type DocumentSummary,
} from "@/lib/api/documents.ts";
import { reauthenticateOnUnauthorized } from "@/lib/auth/redirect.ts";
import { loginPath, nowSeconds, readSession } from "@/lib/auth/server.ts";
import { DOCUMENT_MAX_BYTES } from "@/lib/documents/upload-handler.ts";
import { formatDate, formatSize, formatTime } from "@/lib/format.ts";
import { isCanonicalUuid } from "@/lib/ids.ts";

import { Toolbar } from "../../../../toolbar.tsx";

import { UploadPanel } from "./upload-panel.tsx";

export const dynamic = "force-dynamic";

const MAX_MIB = DOCUMENT_MAX_BYTES / (1024 * 1024);

// Fixed messages for the codes the upload route redirects with; any other value
// is ignored, so nothing from the URL is ever rendered. Upload errors are shown
// next to the file field; page errors at the top.
const SUCCESS: Record<string, string> = {
  "1": "Document uploaded. Open it below to process it.",
};
const UPLOAD_ERRORS: Record<string, string> = {
  too_large: `The file is larger than ${MAX_MIB} MiB.`,
  not_pdf: "Only PDF files can be uploaded.",
  invalid_name:
    "The file name cannot be used. Rename the file (no control characters, at most 255 characters) and try again.",
  missing_file: "Choose a PDF file to upload.",
  unavailable: "The document could not be uploaded right now. Try again.",
  // Malware scanning: the API decided and nothing was stored.
  malware_detected:
    "The malware scan found a threat in this file. It was not stored.",
  scan_incomplete:
    "This file could not be fully scanned (too large or complex when unpacked). It was not stored.",
  encrypted_pdf:
    "Password-protected or encrypted PDFs cannot be uploaded. Remove the protection and try again.",
  scan_unavailable:
    "Malware scanning is unavailable right now, so the file was not stored. Try again later.",
  upload_rejected: "The file was rejected and not stored.",
};
const PAGE_ERRORS: Record<string, string> = {
  rejected:
    "The API rejected this sign-in. Sign out and try again from your account page.",
  // A download of a version that is not clean (the API answered 409).
  not_scanned:
    "This document has not been scanned for malware, so it cannot be downloaded.",
  blocked:
    "This document is blocked by the malware scan, so it cannot be downloaded.",
};

// The API's scan status of each version; only clean versions can be downloaded.
// The list does not carry a document's processing or review status (that is on
// its own page), so nothing here says whether a document has been processed.
const SCAN_LABELS: Record<DocumentSummary["scanStatus"], string> = {
  clean: "Clean",
  not_scanned: "Not scanned",
  blocked: "Blocked",
};
const SCAN_BADGES: Record<DocumentSummary["scanStatus"], string> = {
  clean: "badge badge--success",
  not_scanned: "badge badge--attention",
  blocked: "badge badge--failed",
};

interface Props {
  params: Promise<{ tenantId: string; workspaceId: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

// Server Component: reads the session, never changes cookies. The API decides
// membership on every call; this page only renders its answer.
export default async function WorkspacePage({ params, searchParams }: Props) {
  const { tenantId, workspaceId } = await params;
  if (!isCanonicalUuid(tenantId) || !isCanonicalUuid(workspaceId)) notFound();
  const path = `/tenants/${tenantId}/workspaces/${workspaceId}`;

  const session = await readSession();
  if (!session) redirect(loginPath(path));

  const base = process.env.API_BASE_URL;
  const workspace = await getWorkspace(
    base,
    session.accessToken,
    tenantId,
    workspaceId,
  );
  if (workspace.state === "not_found") notFound();
  const documents =
    workspace.state === "ok"
      ? await listDocuments(base, session.accessToken, tenantId, workspaceId)
      : workspace;
  if (documents.state === "not_found") notFound();
  const rejected =
    workspace.state === "unauthorized" || documents.state === "unauthorized";
  if (rejected && reauthenticateOnUnauthorized(session, nowSeconds())) {
    redirect(loginPath(path));
  }

  const query = await searchParams;
  const success = lookup(SUCCESS, query.uploaded);
  const showForm = !rejected && documents.state === "ok";
  const uploadError = lookup(UPLOAD_ERRORS, query.error);
  // Without the form (API outage or rejected sign-in), an upload error is
  // shown at the top instead.
  const error = rejected
    ? PAGE_ERRORS.rejected
    : (lookup(PAGE_ERRORS, query.error) ??
      (showForm ? undefined : uploadError));

  const workspaceName =
    workspace.state === "ok" ? workspace.workspace.name : "Workspace";
  const count = documents.state === "ok" ? documents.documents.length : 0;
  return (
    <main id="main" tabIndex={-1} className="page">
      <Toolbar
        crumbs={[
          { label: "Workspaces", href: `/tenants/${tenantId}/workspaces` },
          { label: workspaceName },
        ]}
      >
        {showForm && (
          <a className="button-primary" href="#upload">
            <UploadIcon />
            Upload
          </a>
        )}
      </Toolbar>
      <div className="page-body">
        <h1>{workspaceName}</h1>

        {success && <p role="status">{success}</p>}
        {error && <p role="alert">{error}</p>}

        {showForm && (
          <UploadPanel
            action={`${path}/documents/upload`}
            maxMib={MAX_MIB}
            error={uploadError}
            first={count === 0}
          />
        )}

        <section className="panel" aria-labelledby="documents-heading">
          <h2 id="documents-heading">Documents</h2>
          {documents.state === "ok" && count > 0 && (
            <p className="panel-count">
              {count === 1 ? "1 document" : `${count} documents`}
            </p>
          )}
          {documents.state === "ok" ? (
            <DocumentTable
              path={path}
              documents={documents.documents}
              highlightNewest={Boolean(success)}
            />
          ) : documents.state === "unavailable" ? (
            <p role="alert">
              Documents are unavailable right now. <a href={path}>Try again</a>.
            </p>
          ) : null}
        </section>
      </div>
    </main>
  );
}

function DocumentTable({
  path,
  documents,
  highlightNewest,
}: {
  path: string;
  documents: DocumentSummary[];
  highlightNewest: boolean;
}) {
  if (documents.length === 0) {
    return <p className="empty-note">No documents yet.</p>;
  }
  return (
    <table className="documents">
      <caption className="hint">
        Newest first. This list shows the malware scan of each file; open a
        document to process it, follow its status and review it. SHA-256
        identifies the exact stored bytes.
      </caption>
      <thead>
        <tr>
          <th scope="col">Document</th>
          <th scope="col">Malware scan</th>
          <th scope="col" className="open">
            <span className="visually-hidden">Open</span>
          </th>
          <th scope="col" className="uploaded">
            Uploaded
          </th>
          <th scope="col" className="sha">
            SHA-256
          </th>
          <th scope="col">
            <span className="visually-hidden">Download</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {documents.map((document, index) => (
          <tr
            key={document.id}
            className={highlightNewest && index === 0 ? "row--new" : undefined}
          >
            <th scope="row" className="name">
              <FileIcon />
              <span className="name-text">
                <a href={`${path}/documents/${document.id}`}>{document.name}</a>
                <span className="document-row-meta">
                  {formatSize(document.sizeBytes)}
                  <span className="doc-time">
                    {" "}
                    ·{" "}
                    <time
                      dateTime={document.createdAt}
                      title={formatTime(document.createdAt)}
                    >
                      {formatDate(document.createdAt)}
                    </time>
                  </span>
                </span>
              </span>
            </th>
            <td>
              <span className={SCAN_BADGES[document.scanStatus]}>
                {SCAN_LABELS[document.scanStatus]}
              </span>
            </td>
            <td className="open">
              <a href={`${path}/documents/${document.id}`}>
                Open
                <span className="visually-hidden"> {document.name}</span>
              </a>
            </td>
            <td className="uploaded">
              <time
                dateTime={document.createdAt}
                title={formatTime(document.createdAt)}
              >
                {formatDate(document.createdAt)}
              </time>
            </td>
            <td className="sha">
              <code title={document.sha256}>
                {document.sha256.slice(0, 12)}…
              </code>
            </td>
            <td className="download">
              {document.scanStatus === "clean" ? (
                <a href={`${path}/documents/${document.id}/download`}>
                  Download
                  <span className="visually-hidden"> {document.name}</span>
                </a>
              ) : (
                <span className="hint">
                  Not available
                  <span className="visually-hidden">
                    {" "}
                    for {document.name}: {SCAN_LABELS[document.scanStatus]}
                  </span>
                </span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function UploadIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M8 11V3M4.5 6.5L8 3l3.5 3.5M3 13h10" />
    </svg>
  );
}

function FileIcon() {
  return (
    <svg
      className="file-icon"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M4 1.75h5l3 3v8.5a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-10.5a1 1 0 0 1 1-1z" />
      <path d="M9 1.75v3h3" />
    </svg>
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
