# heldout-3 and assist-dev: frozen specification (AI1, assisted-2)

Written and committed before any heldout-3 or assist-dev case exists, and before any assisted-2 prompt, verifier or configuration exists. The machine-readable record is [`heldout-3-spec.json`](heldout-3-spec.json); a test pins both files.

## Why a third held-out split
heldout-2 measured assisted-1 once (not enabled: 2 wrong values against rules-1's 0, from defined short party names quoted verbatim). It is historical evidence only: never used to develop, tune, re-score or measure assisted-2. assisted-2 is measured once on heldout-3, a new split built by a seeded generator from this specification. The known failure is used only as a failure class (party-name completeness), never as a case to reproduce.

## Generation
- `corpus/generated.py` (standard library only), seeded (`seed` in the JSON record): fictional organisation names, places and values from fixed word lists; a template per case.
- Ground truth by construction: every expected output is derived from the parameters the generator used to write the text (the value, its written forms and where each is stated), following the frozen `service_agreement.v1` contract and its candidate rule. No extractor is run to write expected outputs.
- Every expected evidence is checked against the parser's output for the case (parse golden) and every expected output against the contract model, by tests.
- assist-dev and heldout-3 use disjoint templates (no phrasing shared), different seeds-derived names and values, and different id prefixes (`ad-`, `h3-`).

## heldout-3 (24 cases)
| Category | Cases | What it tests |
|---|---|---|
| entity | 10 | full legal name only (unseen wording); defined short name after the full name; trading-as name; full name wrapped across lines; full and short mentions repeated; full name only in the signature block; no longer form anywhere (a one-word name is the stated name); two entities sharing a prefix (one not a party); reversed role wording (the customer named first); a planted definition in instruction-like text |
| phrasing | 3 | dates, amounts and notice periods under labels and clauses the rules do not use |
| layout | 2 | values in a two-column layout; a value on the line after its label |
| missing | 3 | a field named with no value (unparseable); a value only referred to elsewhere (not found); a fee given only as a formula (not found) |
| conflict | 2 | two different end dates; two different notice periods |
| ambiguous | 1 | a slashed numeric date valid day-first and month-first |
| adversarial | 2 | an instruction naming different values; a value inside a quoted example |
| duplicate | 1 | the same value written in two forms |

## assist-dev (10 cases)
Development only (prompt and verifier work for assisted-2), never part of a final measurement: 6 entity cases (definition and shorthand, trading-as, wrapped name, signature-only full name, reversed roles, one-word name) and 4 general cases (an unseen label, a value on the next line, a reference elsewhere, a planted instruction), each from its own template. Its gates for assisted-2 development: integrity, 0 wrong values, 0 wrong party names. rules-1 is reported on it, not gated to 1.0 (rules cannot read unseen phrasings by design).

## Protocol
- heldout-3's cases, expected outputs and hashes are frozen in `heldout-3-preregistration.json` before assisted-2 exists; a test fails if any changes. Its contents are never read for development after the freeze.
- rules-1 is measured on heldout-3 (`heldout-3-baseline.json`) in the same PR, after the freeze.
- assisted-2's prompt, model, configuration and verifier version are frozen and hashed in a committed file before its first heldout-3 call. The frozen configuration is measured once: three runs over dev, heldout and heldout-3. heldout-2 is never sent; assist-dev is not part of the measurement.
- A later change cannot be measured on heldout-3 again.

## Thresholds (fixed now)
Hard, every run: integrity on every measured split (schema-valid 100 %, citations resolving 100 %, 0 unsupported claims, failure codes exact); dev every accuracy metric 1.0, 0 silent conflicts, 0 wrong values; heldout no regression against `heldout-baseline.json`.

Primary, heldout-3, worst of the three runs, against rules-1 on heldout-3:
- field accuracy at least +0.15; evidence completeness at least +0.15;
- abstention recall and conflict recall not lower;
- wrong values not above rules-1 (global, every field);
- wrong party names (wrong values of provider_name or customer_name) = 0;
- silent conflicts not higher.

Budget per call: p95 latency at most 20 s, mean cost at most USD 0.05.

**Decision rule:** assisted-2 may be enabled only if every hard gate holds on every run, every primary threshold holds on the worst run, and the budget holds. Otherwise it is reported as measured and model assistance is not part of this milestone (no assisted-3); the milestone ships rules-only.

## Limitations
- 24 cases: directional evidence, reported with counts.
- One author writes the templates and assisted-2; mitigated by this frozen specification, generated ground truth, disjoint templates and the freeze order, not removed.
- Generated text is more regular than real agreements.
