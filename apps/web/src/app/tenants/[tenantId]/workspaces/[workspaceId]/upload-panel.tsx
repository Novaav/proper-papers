import { UploadForm } from "./upload-form.tsx";

// The upload area, once, on the page where documents are added: a drop zone
// that is always there, large in an empty workspace and compact beside a list.
// The toolbar's Upload button is a plain link to it (#upload). It is one form
// and one flow: the checks, the malware scan and every message are the
// server's, unchanged.
export function UploadPanel({
  action,
  maxMib,
  error,
  first,
}: {
  action: string;
  maxMib: number;
  error?: string | undefined;
  /** No documents yet: the page's one call to action. */
  first: boolean;
}) {
  return (
    <section
      id="upload"
      className={first ? "upload-panel upload-panel--first" : "upload-panel"}
      aria-labelledby="upload-heading"
    >
      <h2 id="upload-heading">
        {first ? "Upload your first document" : "Upload a document"}
      </h2>
      {first && (
        <p className="hint">
          Upload a PDF to start. It is scanned for malware first and stored only
          if the scan finds nothing; then you can process it into a record with
          its evidence.
        </p>
      )}
      <UploadForm action={action} maxMib={maxMib} error={error} />
    </section>
  );
}
