"use client";

import { useEffect, useReducer, useRef, useState } from "react";

import { formatSize } from "@/lib/format.ts";

import { DocumentScan, IndeterminateBar } from "../../../../loading-states.tsx";

import { initialUploadState, uploadReducer } from "./upload-state.ts";

// A plain HTML multipart form (it works without JavaScript). With JavaScript,
// submitting disables the button and announces progress, which also prevents
// double submits; the state resets when the page is restored from history.
// An error from the previous upload is rendered next to the file field (also
// without JavaScript) and stays until the user chooses a file or submits again.
export function UploadForm({
  action,
  maxMib,
  error,
}: {
  action: string;
  maxMib: number;
  /** Fixed message for the previous upload's error code, if any. */
  error?: string | undefined;
}) {
  const [state, dispatch] = useReducer(
    uploadReducer,
    Boolean(error),
    initialUploadState,
  );

  // Opened from the address (#upload): put the cursor in the file field.
  useEffect(() => {
    const focusIfOpened = () => {
      if (window.location.hash === "#upload") {
        document.getElementById("document-file")?.focus();
      }
    };
    focusIfOpened();
    window.addEventListener("hashchange", focusIfOpened);
    return () => window.removeEventListener("hashchange", focusIfOpened);
  }, []);

  useEffect(() => {
    const onPageShow = () => dispatch("page_shown");
    window.addEventListener("pageshow", onPageShow);
    return () => window.removeEventListener("pageshow", onPageShow);
  }, []);

  // The file the user picked, shown back to them before anything is sent.
  const [chosen, setChosen] = useState<{ name: string; size: number } | null>(
    null,
  );

  // Drag and drop is only another way to choose the file: the dropped file is
  // put into the same file field, and the form is sent by pressing Upload like
  // before. Every check (type, size, scan) is still the server's.
  const field = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const chooseDropped = (files: FileList | null) => {
    const file = files?.[0];
    if (!file || !field.current) return;
    const transfer = new DataTransfer();
    transfer.items.add(file);
    field.current.files = transfer.files;
    dispatch("file_changed");
    setChosen({ name: file.name, size: file.size });
  };

  const showError = Boolean(error) && state.errorVisible;
  const invalid = showError || state.requiredFailed;

  return (
    <form
      method="post"
      action={action}
      encType="multipart/form-data"
      onSubmit={() => dispatch("submitted")}
      aria-busy={state.uploading || undefined}
    >
      <label htmlFor="document-file">PDF file</label>
      <div
        className={
          state.uploading
            ? "dropzone dropzone--busy"
            : over
              ? "dropzone dropzone--over"
              : chosen
                ? "dropzone dropzone--chosen"
                : "dropzone"
        }
        onDragOver={(event) => {
          event.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(event) => {
          event.preventDefault();
          setOver(false);
          chooseDropped(event.dataTransfer.files);
        }}
      >
        <input
          ref={field}
          id="document-file"
          name="file"
          type="file"
          accept="application/pdf,.pdf"
          required
          aria-describedby={
            showError
              ? "document-file-hint document-file-error"
              : "document-file-hint"
          }
          // Explicit in both states: with only `required`, browsers expose the
          // empty field as invalid before any interaction (native :invalid).
          aria-invalid={invalid}
          onChange={() => dispatch("file_changed")}
          onInput={(event) => {
            const file = event.currentTarget.files?.[0];
            setChosen(file ? { name: file.name, size: file.size } : null);
          }}
          onInvalid={() => dispatch("validation_failed")}
        />
        <span className="dropzone-text">Drop a PDF here, or choose one</span>
        {chosen && (
          <p className="file-chosen" aria-live="polite">
            Selected: <strong>{chosen.name}</strong> · {formatSize(chosen.size)}
          </p>
        )}
      </div>
      <p id="document-file-hint" className="hint">
        PDF only, up to {maxMib} MiB. Each file is scanned for malware before it
        is stored and is stored only if the scan finds nothing. Encrypted PDFs
        are not accepted. Stored files are kept exactly as uploaded.
      </p>
      {showError && (
        <p id="document-file-error" role="alert" className="error">
          {error}
        </p>
      )}
      <button
        type="submit"
        className="button-primary"
        disabled={state.uploading}
      >
        {state.uploading ? "Uploading…" : "Upload"}
      </button>
      {/* The browser reports no progress for a plain form post, so there is no
          percentage: an indeterminate bar, and a sentence that names the file. */}
      <p role="status" className="hint">
        {state.uploading
          ? `Uploading ${chosen ? `${chosen.name} (${formatSize(chosen.size)})` : "the file"}. It is scanned before it is stored, and large files can take a moment.`
          : ""}
      </p>
      {state.uploading && (
        <div className="upload-progress">
          <DocumentScan state="running" />
          <IndeterminateBar label="Uploading the document" />
        </div>
      )}
    </form>
  );
}
