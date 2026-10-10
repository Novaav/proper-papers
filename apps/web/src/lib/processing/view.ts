// What the document page shows for a processing run and its result: pure
// functions over the API's service_agreement.v1 contract, so every state and
// every sentence is unit-tested. Nothing here invents a value: unknown fields
// say why they are unknown, conflicts list every candidate with its own
// evidence, and no candidate is ever preferred.

import {
  FIELD_KEYS,
  type Candidate,
  type ExtractionResult,
  type Field,
  type FieldKey,
  type FieldValue,
  type ProcessingRun,
} from "../api/processing.ts";
import { formatTime } from "../format.ts";
import { liveStatusText, type StatusTone } from "./status-text.ts";

export { finishedAnnouncement, liveStatusText } from "./status-text.ts";

export const FIELD_LABELS: Record<FieldKey, string> = {
  provider_name: "Provider",
  customer_name: "Customer",
  effective_date: "Effective date",
  end_date: "End date",
  governing_law: "Governing law",
  contract_value: "Contract value",
  notice_period_days: "Notice period",
};

/** A missing required field, or any conflict, needs review (the API's rule). */
export const REQUIRED_FIELDS: ReadonlySet<FieldKey> = new Set([
  "provider_name",
  "customer_name",
  "effective_date",
]);

const KIND: Record<FieldKey, string> = {
  provider_name: "name",
  customer_name: "name",
  effective_date: "date",
  end_date: "date",
  governing_law: "jurisdiction",
  contract_value: "amount",
  notice_period_days: "number of days",
};

export interface QuoteView {
  page: number;
  /** The quote split around the first occurrence of the raw value. */
  before: string;
  marked: string;
  after: string;
}

export interface CandidateView {
  valueText: string;
  /** False for an unparseable or ambiguous mention (no value). */
  resolved: boolean;
  quotes: QuoteView[];
}

export interface FieldRowView {
  key: FieldKey;
  id: string;
  label: string;
  required: boolean;
  status: Field["status"];
  /** The main line: the value, or a sentence saying why there is none. */
  valueText: string;
  /** Short text of the disclosure ("Page 1", "Pages 1, 3", "Compare 2"); null if no evidence. */
  sourceText: string | null;
  /** Contributes to "needs review" (conflict, or a required field without a value). */
  attention: boolean;
  /** Shown open: every row whose evidence explains something unusual. */
  open: boolean;
  /** Why the row needs attention, in words (for the summary). */
  attentionText: string | null;
  candidates: CandidateView[];
}

export interface RecordView {
  outcome: ExtractionResult["outcome"];
  rows: FieldRowView[];
  attention: FieldRowView[];
  found: number;
}

export function recordView(result: ExtractionResult): RecordView {
  const rows = FIELD_KEYS.map((key) => fieldRow(key, result.fields[key]));
  return {
    outcome: result.outcome,
    rows,
    attention: rows.filter((row) => row.attention),
    found: rows.filter((row) => row.status === "found").length,
  };
}

export function fieldRow(key: FieldKey, field: Field): FieldRowView {
  const label = FIELD_LABELS[key];
  const required = REQUIRED_FIELDS.has(key);
  const candidates = field.candidates.map((candidate) =>
    candidateView(key, candidate),
  );
  const base = {
    key,
    id: `field-${key.replaceAll("_", "-")}`,
    label,
    required,
    status: field.status,
    candidates,
  };
  if (field.status === "found") {
    return {
      ...base,
      valueText: candidates[0]?.valueText ?? "",
      sourceText: pagesText(field.candidates),
      attention: false,
      open: false,
      attentionText: null,
    };
  }
  if (field.status === "conflict") {
    return {
      ...base,
      valueText: `${candidates.length} different values`,
      sourceText: `Compare ${candidates.length}`,
      attention: true,
      open: true,
      attentionText: `${candidates.length} different values found`,
    };
  }
  const missing = required ? " (required)" : "";
  switch (field.reason) {
    case "unparseable":
      return {
        ...base,
        valueText: `Found, but not a valid ${KIND[key]}`,
        sourceText: pagesText(field.candidates),
        attention: required,
        open: true,
        attentionText: required ? `not a valid ${KIND[key]}${missing}` : null,
      };
    case "ambiguous":
      return {
        ...base,
        valueText: "Ambiguous: it can be read in more than one way",
        sourceText: pagesText(field.candidates),
        attention: required,
        open: true,
        attentionText: required ? `ambiguous${missing}` : null,
      };
    default:
      return {
        ...base,
        valueText: "Not found in the document",
        sourceText: null,
        attention: required,
        open: false,
        attentionText: required ? `not found${missing}` : null,
      };
  }
}

function candidateView(key: FieldKey, candidate: Candidate): CandidateView {
  return {
    valueText:
      candidate.value === null
        ? `“${candidate.raw}” (not a valid ${KIND[key]})`
        : formatValue(key, candidate.value),
    resolved: candidate.value !== null,
    quotes: candidate.evidence.map((span) => {
      const at = span.quote.indexOf(candidate.raw);
      return at < 0
        ? { page: span.page, before: span.quote, marked: "", after: "" }
        : {
            page: span.page,
            before: span.quote.slice(0, at),
            marked: candidate.raw,
            after: span.quote.slice(at + candidate.raw.length),
          };
    }),
  };
}

function pagesText(candidates: Candidate[]): string {
  const spans = candidates.flatMap((candidate) => candidate.evidence);
  const pages = [...new Set(spans.map((span) => span.page))].sort(
    (a, b) => a - b,
  );
  const text =
    pages.length === 1 ? `Page ${pages[0]}` : `Pages ${pages.join(", ")}`;
  return spans.length > pages.length
    ? `${text} · ${spans.length} quotes`
    : text;
}

const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

/** A normalized value as a person reads it; the exact source text stays in the quotes. */
export function formatValue(key: FieldKey, value: FieldValue): string {
  if (typeof value === "number") {
    return `${value} ${value === 1 ? "day" : "days"}`;
  }
  if (typeof value === "string") {
    if (key === "effective_date" || key === "end_date") {
      const [year, month, day] = value.split("-").map(Number);
      return `${day} ${MONTHS[(month ?? 1) - 1]} ${year}`;
    }
    return value;
  }
  // Grouped by hand: amounts up to 15 digits exceed what a float keeps exactly.
  const [whole = "", cents = "00"] = value.amount.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${value.currency} ${grouped}.${cents}`;
}

// --- The processing state line -----------------------------------------------

export interface StatusView {
  tone: StatusTone;
  /** One sentence; the state in words (never colour alone). */
  text: string;
  /** Still changing: the page checks again while this is true. */
  live: boolean;
}

export function statusView(
  run: ProcessingRun | null,
  record: RecordView | null,
): StatusView {
  if (run === null) {
    return { tone: "neutral", text: "Not processed yet.", live: false };
  }
  switch (run.status) {
    case "queued":
    case "running":
      return {
        tone: "working",
        text: liveStatusText(run.status, run.attempts, run.startedAt),
        live: true,
      };
    case "completed":
      return {
        tone: "neutral",
        text: `Processed${run.finishedAt ? ` ${formatTime(run.finishedAt)}` : ""}.`,
        live: false,
      };
    case "needs_review": {
      const count = record?.attention.length ?? 0;
      return {
        tone: "attention",
        text: `Needs review — ${count} ${count === 1 ? "field" : "fields"}.`,
        live: false,
      };
    }
    case "failed":
      return {
        tone: "failed",
        text: failureMessage(run.failureCode),
        live: false,
      };
  }
}

const FAILURES: Record<string, string> = {
  no_text_layer:
    "Processing failed: the document has no text layer (for example a scanned image). Text recognition is not supported.",
  embedded_content:
    "Processing failed: the document contains attached or embedded files, which are not processed.",
  encrypted_pdf: "Processing failed: the document is encrypted.",
  parse_failed: "Processing failed: the document could not be read as a PDF.",
  too_many_pages:
    "Processing failed: the document has more pages than can be processed (100).",
  text_too_large:
    "Processing failed: the document has more text than can be processed.",
  input_too_large: "Processing failed: the document is too large to process.",
  timeout: "Processing failed: the document took too long to process.",
  memory_exceeded:
    "Processing failed: the document needed more memory than is allowed.",
  not_clean:
    "Processing failed: the document is no longer cleared by the malware scan.",
  integrity_mismatch:
    "Processing failed: the stored file no longer matches its checksum.",
};

export function failureMessage(code: string | null): string {
  return (
    (code !== null && Object.hasOwn(FAILURES, code) ? FAILURES[code] : null) ??
    "Processing failed: the processing service could not complete this document."
  );
}
