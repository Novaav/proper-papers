import { readSession } from "@/lib/auth/server.ts";

import { Reveal } from "./reveal.tsx";

const PROMISES = [
  {
    title: "Evidence for every value",
    text: "Each extracted value links to the exact text on the page it came from.",
  },
  {
    title: "Unknown, never invented",
    text: "When a document does not state a value, the field stays unknown.",
  },
  {
    title: "Conflicts in plain sight",
    text: "When a document states two different values, both are shown; neither is chosen for you.",
  },
  {
    title: "Reviewed before export",
    text: "A reviewer decides the flagged fields and approves an immutable version, exported as JSON.",
  },
];

const STEPS = [
  {
    title: "Upload",
    text: "Add a PDF to a workspace. It is scanned for malware before it is stored.",
  },
  {
    title: "Extract",
    text: "The PDF is read in an isolated sandbox, and a service-agreement record is extracted with its evidence.",
  },
  {
    title: "Review",
    text: "A reviewer decides every flagged field from its evidence.",
  },
  {
    title: "Export",
    text: "Approve an immutable version and download it as the same JSON every time.",
  },
];

// The hero action follows the session like the header does: "Sign in" without
// one, "Open workspace" with one. Both go to /account, which shows the account
// at once when a session exists and sends the browser to sign in when it does
// not, so the label is the only difference.
export default async function HomePage() {
  const signedIn = (await readSession()) !== null;
  return (
    <main id="main" tabIndex={-1} className="landing">
      <section className="hero" aria-labelledby="title">
        <div className="hero-copy">
          <p className="eyebrow">Evidence-grounded document intelligence</p>
          <h1 id="title">
            Structured records you can check,
            <span className="soft"> value by value.</span>
          </h1>
          <p className="lead">
            Proper Papers turns service agreements into structured records in
            which every value links to its source text, missing values stay
            unknown, and conflicts are surfaced for human review.
          </p>
          <p className="hero-actions">
            <a className="button-primary" href="/account">
              {signedIn ? "Open workspace" : "Sign in"}
            </a>
            <a href="#how-title">How it works</a>
          </p>
        </div>
        <ExampleRecord />
      </section>

      <section className="promises" aria-labelledby="promises-title">
        <Reveal>
          <h2 id="promises-title">What you can rely on</h2>
          <ul>
            {PROMISES.map((promise) => (
              <li key={promise.title}>
                <h3>{promise.title}</h3>
                <p>{promise.text}</p>
              </li>
            ))}
          </ul>
        </Reveal>
      </section>

      <section className="how" aria-labelledby="how-title">
        <Reveal>
          <h2 id="how-title">How it works</h2>
          <ol className="steps">
            {STEPS.map((step) => (
              <li key={step.title}>
                <h3>{step.title}</h3>
                <p>{step.text}</p>
              </li>
            ))}
          </ol>
        </Reveal>
      </section>

      <section className="system-status" aria-labelledby="system-status">
        <h2 id="system-status">About this project</h2>
        <p>
          A portfolio project, built and evaluated on synthetic documents. It
          runs locally and is not deployed.
        </p>
      </section>
    </main>
  );
}

// A static picture of the product's central idea, with synthetic content: the
// review workbench as it looks, with a conflict left for a reviewer, each value
// beside its exact source text. Not interactive and not connected to any data.
function ExampleRecord() {
  return (
    <figure className="example" aria-labelledby="example-caption">
      <figcaption id="example-caption" className="example-head">
        <span>
          <span className="example-file">service-agreement.pdf</span>
          <span className="example-note">Example record · synthetic</span>
        </span>
        <span className="badge badge--attention">Needs review</span>
      </figcaption>
      <div className="example-body">
        <ul className="example-fields">
          <li className="ex-row">
            <span className="ex-label">Provider</span>
            <span className="ex-value">Ashgrove Analytics Ltd</span>
          </li>
          <li className="ex-row ex-row--current">
            <span className="ex-label">Effective date</span>
            <span className="ex-value">
              2 different values{" "}
              <span className="badge badge--attention">Conflict</span>
            </span>
          </li>
          <li className="ex-row ex-row--unknown">
            <span className="ex-label">Governing law</span>
            <span className="ex-value">
              Unknown <span className="badge">Not found in the document</span>
            </span>
          </li>
        </ul>
        <div className="example-pane">
          <p className="pane-kicker">Effective date</p>
          <p className="example-title">2 different values</p>
          <p className="provenance">
            Quoted from{" "}
            <span className="provenance-file">service-agreement.pdf</span>
          </p>
          <div className="candidates">
            <div className="candidate">
              <p className="candidate-value">1 March 2026</p>
              <figure className="quote">
                <figcaption>Page 1</figcaption>
                <blockquote>
                  This Agreement commences on <mark>1 March 2026</mark>.
                </blockquote>
              </figure>
            </div>
            <div className="candidate">
              <p className="candidate-value">1 April 2026</p>
              <figure className="quote">
                <figcaption>Page 2</figcaption>
                <blockquote>
                  It is effective as of <mark>1 April 2026</mark>.
                </blockquote>
              </figure>
            </div>
          </div>
        </div>
      </div>
    </figure>
  );
}
