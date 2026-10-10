import type { FieldKey } from "@/lib/api/processing.ts";
import { formatTime } from "@/lib/format.ts";
import type {
  CandidateView,
  FieldRowView,
  QuoteView,
  RecordView,
} from "@/lib/processing/view.ts";
import { afterDecision } from "@/lib/review/advance.ts";
import type {
  FieldReview,
  ReviewAction,
  ReviewView,
} from "@/lib/review/view.ts";

import { WorkbenchSync } from "./workbench-sync.tsx";

// The extracted record as a workbench: the fields on one side, and for the
// field you are looking at, its evidence and the decisions on the other.
//
// Every field is a link to its panel (#field-…); the panel the address points
// at is shown, or else the first field that needs review. That is plain HTML
// and CSS, so it works without JavaScript and on any screen: wide screens put
// the panel beside the list, narrow ones under it. Nothing here changes what
// the record says: each panel shows the exact quotes (page and text, with the
// value marked), UNKNOWN as UNKNOWN, conflicting values side by side, and the
// same decision forms as before.
//
// Review (R1): a reviewer's decisions are plain forms next to the evidence
// they point at (choose an extracted value, or mark the field unknown; never
// free text), and the approval panel at the top says what is left to decide,
// approves, and links each approved version.
export function Record({
  record,
  review,
  reviewUnavailable,
  path,
  source,
}: {
  record: RecordView;
  review: ReviewView | null;
  reviewUnavailable: boolean;
  path: string;
  /** The document's name: where every quote comes from. */
  source: string;
}) {
  const open = (row: FieldRowView) =>
    row.attention && !(review?.fields[row.key]?.resolved ?? false);
  const needs = record.rows.filter(open);
  const others = record.rows.filter((row) => !open(row));
  // Opens on the first field that needs a decision and has source text to
  // show; failing that, the first that needs a decision; failing that, the
  // first with source text.
  const first =
    needs.find((row) => row.sourceText !== null) ??
    needs[0] ??
    record.rows.find((row) => row.sourceText !== null) ??
    record.rows[0];
  // The default panel comes last in the document: "a later sibling of the
  // targeted panel is hidden" is how CSS shows one panel at a time.
  const paneRows = [
    ...record.rows.filter((row) => row.id !== first?.id),
    ...record.rows.filter((row) => row.id === first?.id),
  ];
  const labels = Object.fromEntries(
    record.rows.map((row) => [row.id, row.label]),
  );
  return (
    <section className="record" aria-labelledby="record-heading">
      <h2 id="record-heading">Service agreement</h2>
      {review ? (
        <ApprovalPanel record={record} review={review} path={path} />
      ) : (
        <Summary record={record} rows={record.attention} />
      )}
      {reviewUnavailable && (
        <p className="review-note">Review is unavailable right now.</p>
      )}
      <div className="workbench" data-default={first?.id}>
        <nav className="field-list" aria-label="Fields of the record">
          {needs.length > 0 && (
            <FieldGroup
              title="Needs review"
              rows={needs}
              first={first}
              review={review}
            />
          )}
          <FieldGroup
            title={needs.length > 0 ? "Other fields" : "Fields"}
            rows={others}
            first={first}
            review={review}
          />
        </nav>
        <div className="panes">
          {paneRows.map((row) => (
            <Pane
              key={row.key}
              next={nextAfter(row, record.rows, needs, review)}
              after={afterDecision(row, record.rows, needs)}
              row={row}
              isDefault={row.id === first?.id}
              review={review}
              fieldReview={review?.fields[row.key] ?? null}
              path={path}
              source={source}
            />
          ))}
        </div>
        <WorkbenchSync labels={labels} />
      </div>
    </section>
  );
}

function FieldGroup({
  title,
  rows,
  first,
  review,
}: {
  title: string;
  rows: FieldRowView[];
  first: FieldRowView | undefined;
  review: ReviewView | null;
}) {
  if (rows.length === 0) return null;
  return (
    <>
      <h3 className="group-title">
        {title} <span className="rail-count">{rows.length}</span>
      </h3>
      <ul className="fields">
        {rows.map((row) => (
          <FieldItem
            key={row.key}
            row={row}
            isDefault={row.id === first?.id}
            fieldReview={review?.fields[row.key] ?? null}
          />
        ))}
      </ul>
    </>
  );
}

function Summary({
  record,
  rows,
}: {
  record: RecordView;
  rows: FieldRowView[];
}) {
  if (rows.length === 0) {
    return (
      <p className="record-summary">
        {record.outcome === "needs_review"
          ? "Every field that needed review is decided"
          : "All required fields found"}{" "}
        · {record.found} of {record.rows.length} fields found.
      </p>
    );
  }
  return (
    <div className="review">
      <p className="review-title">
        Review needed — {rows.length} of {record.rows.length} fields:
      </p>
      <ul>
        {rows.map((row) => (
          <li key={row.key}>
            <a href={`#${row.id}`}>{row.label}</a>: {row.attentionText}
          </li>
        ))}
      </ul>
    </div>
  );
}

function ApprovalPanel({
  record,
  review,
  path,
}: {
  record: RecordView;
  review: ReviewView;
  path: string;
}) {
  const [newest, ...earlier] = review.versions;
  const exportHref = (version: number) =>
    `${path}/review/approvals/${version}/export`;
  return (
    <div className="approval" id="approval">
      {review.canApprove && !newest?.current && (
        <p className="review-title review-title--ready">Ready for approval</p>
      )}
      <Summary record={record} rows={review.unresolved} />
      {review.canReview && review.unresolved[0] && (
        <a
          className="button-primary band-action"
          href={`#${review.unresolved[0].id}`}
        >
          Review {review.unresolved.length}{" "}
          {review.unresolved.length === 1 ? "field" : "fields"}
        </a>
      )}
      {newest && (
        <p className="approval-version">
          {newest.current
            ? `Approved as version ${newest.version}`
            : `Changed since version ${newest.version} was approved`}{" "}
          ·{" "}
          <time dateTime={newest.approvedAt}>
            {formatTime(newest.approvedAt)}
          </time>{" "}
          ·{" "}
          <a href={exportHref(newest.version)}>
            Download JSON
            <span className="visually-hidden"> (version {newest.version})</span>
          </a>
        </p>
      )}
      {review.canApprove ? (
        <form method="post" action={`${path}/review/approvals`}>
          <input type="hidden" name="run_id" value={review.runId} />
          <input type="hidden" name="expected_seq" value={review.expectedSeq} />
          <input
            type="hidden"
            name="expected_version"
            value={review.expectedVersion}
          />
          <button type="submit" className="button-primary">
            {newest
              ? `Approve again as version ${review.expectedVersion + 1}`
              : "Approve record"}
          </button>
        </form>
      ) : (
        review.canReview &&
        review.unresolved.length > 0 && (
          <p className="review-note">
            Decide the fields marked Needs review to approve the record.
          </p>
        )
      )}
      {!review.canReview && (
        <p className="review-note">Only reviewers can review and approve.</p>
      )}
      {earlier.length > 0 && (
        <details className="approval-earlier">
          <summary>Earlier versions</summary>
          <ul>
            {earlier.map((version) => (
              <li key={version.version}>
                Version {version.version} ·{" "}
                <time dateTime={version.approvedAt}>
                  {formatTime(version.approvedAt)}
                </time>{" "}
                ·{" "}
                <a href={exportHref(version.version)}>
                  Download JSON
                  <span className="visually-hidden">
                    {" "}
                    (version {version.version})
                  </span>
                </a>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function FieldItem({
  row,
  isDefault,
  fieldReview,
}: {
  row: FieldRowView;
  isDefault: boolean;
  fieldReview: FieldReview | null;
}) {
  const resolved = fieldReview?.resolved ?? false;
  const attention = row.attention && !resolved;
  const className = [
    "field",
    attention ? "field--attention" : "",
    row.status === "found" ? "" : "field--unresolved",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <li className={className}>
      <a
        className="field-link"
        href={`#${row.id}`}
        aria-current={isDefault ? "true" : undefined}
      >
        <span className="field-label">{row.label}</span>
        <span className="field-value">
          {row.valueText}
          {attention && <span className="field-flag"> Needs review</span>}
          {fieldReview?.decisionText && (
            <span
              className={`field-decision field-decision--${fieldReview.decisionTone}`}
            >
              {fieldReview.decisionText}
            </span>
          )}
        </span>
        {row.sourceText !== null && (
          <span className="field-source">{row.sourceText}</span>
        )}
      </a>
    </li>
  );
}

function Pane({
  row,
  next,
  after,
  isDefault,
  review,
  fieldReview,
  path,
  source,
}: {
  row: FieldRowView;
  next: { href: string; text: string } | null;
  /** Where a saved decision on this field takes the reviewer (see afterDecision). */
  after: string;
  isDefault: boolean;
  review: ReviewView | null;
  fieldReview: FieldReview | null;
  path: string;
  source: string;
}) {
  const resolved = fieldReview?.resolved ?? false;
  const attention = row.attention && !resolved;
  const actions =
    review && fieldReview && fieldReview.otherActions.length > 0 ? (
      <div className="review-actions">
        {fieldReview.otherActions.map((action) => (
          <ActionForm
            key={action.label}
            action={action}
            field={row.key}
            after={after}
            review={review}
            path={path}
          />
        ))}
      </div>
    ) : null;
  return (
    <section
      id={row.id}
      className={isDefault ? "pane pane--default" : "pane"}
      aria-labelledby={`${row.id}-title`}
    >
      <header className="pane-head">
        <p className="pane-kicker">{row.label}</p>
        <h3 id={`${row.id}-title`} className="pane-value">
          {row.valueText}
          {attention && <span className="field-flag"> Needs review</span>}
        </h3>
        {fieldReview?.decisionText && (
          <p
            className={`field-decision field-decision--${fieldReview.decisionTone}`}
          >
            {fieldReview.decisionText}
          </p>
        )}
      </header>
      {row.sourceText === null ? (
        <p className="pane-empty">There is no source text for this field.</p>
      ) : (
        <div className="evidence">
          <p className="provenance">
            Quoted from <span className="provenance-file">{source}</span>
          </p>
          {row.status === "conflict" ? (
            <ol className="candidates">
              {row.candidates.map((candidate, index) => {
                const action = fieldReview?.candidateActions[index] ?? null;
                return (
                  <li key={index} className="candidate">
                    <p className="candidate-value">{candidate.valueText}</p>
                    <Quotes candidate={candidate} />
                    {review && action && (
                      <ActionForm
                        action={action}
                        field={row.key}
                        after={after}
                        review={review}
                        path={path}
                      />
                    )}
                  </li>
                );
              })}
            </ol>
          ) : (
            row.candidates.map((candidate, index) => (
              <Quotes key={index} candidate={candidate} />
            ))
          )}
        </div>
      )}
      {actions}
      {next && (
        <p className="pane-next">
          <a href={next.href}>{next.text}</a>
        </p>
      )}
    </section>
  );
}

// Where the review goes next from a field: the next field that still needs a
// decision (in record order, wrapping round), or, once none is left, the
// approval. Links only; it changes nothing.
function nextAfter(
  row: FieldRowView,
  rows: FieldRowView[],
  needs: FieldRowView[],
  review: ReviewView | null,
): { href: string; text: string } | null {
  const others = needs.filter((candidate) => candidate.id !== row.id);
  if (others.length > 0) {
    const index = rows.findIndex((candidate) => candidate.id === row.id);
    const target =
      others.find((candidate) => rows.indexOf(candidate) > index) ?? others[0];
    return target
      ? { href: `#${target.id}`, text: `Next to review: ${target.label} →` }
      : null;
  }
  if (needs.length === 0 && review?.canApprove) {
    return {
      href: "#approval",
      text: "Every flagged field is decided. Go to the approval →",
    };
  }
  return null;
}

function ActionForm({
  action,
  field,
  after,
  review,
  path,
}: {
  action: ReviewAction;
  field: FieldKey;
  after: string;
  review: ReviewView;
  path: string;
}) {
  return (
    <form
      method="post"
      action={`${path}/review/decisions`}
      className="review-action"
    >
      <input type="hidden" name="run_id" value={review.runId} />
      <input type="hidden" name="expected_seq" value={review.expectedSeq} />
      <input type="hidden" name="field_key" value={field} />
      <input type="hidden" name="resolution" value={action.resolution} />
      {/* Where a saved decision goes next. It only steers the redirect: a
          refused or failed decision stays on this field. */}
      <input type="hidden" name="after" value={after} />
      {action.candidateOrdinal !== null && (
        <input
          type="hidden"
          name="candidate_ordinal"
          value={action.candidateOrdinal}
        />
      )}
      <button type="submit" className="button-secondary">
        {action.label}
        <span className="visually-hidden">{action.context}</span>
      </button>
    </form>
  );
}

function Quotes({ candidate }: { candidate: CandidateView }) {
  return candidate.quotes.map((quote, index) => (
    <Quote key={index} quote={quote} />
  ));
}

function Quote({ quote }: { quote: QuoteView }) {
  return (
    <figure className="quote">
      <figcaption>Page {quote.page}</figcaption>
      <blockquote>
        {quote.before}
        {quote.marked && <mark>{quote.marked}</mark>}
        {quote.after}
      </blockquote>
    </figure>
  );
}
