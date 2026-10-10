// The one loading-state treatment: a miniature page with a soft evidence-yellow
// highlight that travels down its lines, and a thin indeterminate track. Built
// from SVG and CSS (opacity and transform only), and neither piece invents a
// number: the track is indeterminate (the browser reports no upload progress for
// a plain form post), and the page only reflects a state the API actually
// reports ("queued" waiting, "running" being read).

/** A thin track whose moving part says "working", never "how far". Decorative alone. */
export function LoadingTrack({ state }: { state: "queued" | "running" }) {
  return (
    <span className="loading-track" data-state={state} aria-hidden="true">
      <span className="loading-track-run" />
    </span>
  );
}

/** The same track as a named progress bar with no value (an upload in flight). */
export function IndeterminateBar({ label }: { label: string }) {
  return (
    <span role="progressbar" aria-label={label} className="loading-track">
      <span className="loading-track-run" />
    </span>
  );
}

/**
 * A miniature page: waiting in the queue it is still, with one faint line
 * marked; being read, a highlight wipes across its lines in turn. Purely
 * visual (the status text beside it says the same in words), so it is hidden
 * from assistive technology.
 */
export function DocumentScan({ state }: { state: "queued" | "running" }) {
  return (
    <svg
      className="doc-scan"
      data-state={state}
      viewBox="0 0 24 30"
      aria-hidden="true"
      focusable="false"
    >
      <rect
        className="doc-scan-page"
        x="2"
        y="1"
        width="20"
        height="28"
        rx="3"
      />
      <rect
        className="doc-scan-hl doc-scan-hl--1"
        x="5"
        y="6.3"
        width="14"
        height="3.4"
        rx="1.4"
      />
      <rect
        className="doc-scan-hl doc-scan-hl--2"
        x="5"
        y="11.3"
        width="14"
        height="3.4"
        rx="1.4"
      />
      <rect
        className="doc-scan-hl doc-scan-hl--3"
        x="5"
        y="16.3"
        width="9.5"
        height="3.4"
        rx="1.4"
      />
      <rect
        className="doc-scan-hl doc-scan-hl--4"
        x="5"
        y="21.3"
        width="12"
        height="3.4"
        rx="1.4"
      />
      <path
        className="doc-scan-line"
        d="M6.5 8h11M6.5 13h11M6.5 18h7M6.5 23h9"
        strokeWidth="1.5"
        strokeLinecap="round"
        fill="none"
      />
    </svg>
  );
}
