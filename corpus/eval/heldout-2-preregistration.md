# heldout-2: pre-registration (AI1)

Registered before any model-assisted extractor, prompt or provider exists. The machine-readable record, with the case list, the SHA-256 of every expected output and the thresholds, is [`heldout-2-preregistration.json`](heldout-2-preregistration.json); a test fails if any expected output or the case list changes.

## Why a second held-out split
The first held-out split (8 cases) has been read by the author of the rules extractor and is the baseline of D-063. It cannot be an unbiased first measurement of a prompt written by the same author. heldout-2 is new: written systematically from the frozen `service_agreement.v1` contract in categories declared before the cases, with the expected outputs frozen here.

## Coverage (16 cases, 112 fields: 58 found, 2 conflict, 52 unknown)
| Category | Cases | What it tests |
|---|---|---|
| phrasing | p1 labels the contract does not use; p2 running prose; p3 defined short names; p4 month-first dates, whole-dollar amount | values the rules cannot see |
| layout | l1 two-column table; l2 value on the line after its label (a notice period in months: unparseable); l3 a name wrapping onto the next line (the exact quote contains a line break) | verbatim evidence across layouts |
| missing | m1 provider "to be confirmed", never named; m2 value and law only referred to elsewhere; m3 a heading that is not a name | abstention: nothing may be invented |
| conflict | c1 labelled provider vs a different provider in prose on page 2; c2 labelled notice period vs a termination clause | conflicts stay visible |
| ambiguous | a1 a slashed numeric date under an unusual label | no guessing |
| adversarial | x1 an instruction to automated assistants naming a different client and start date (both appear verbatim); x2 a fee given only as a formula | planted values and invented amounts |
| duplicate | d1 the same date in words and ISO form under unseen labels | one value, two evidence spans |

Candidate rule used for the expected outputs (the contract's, made explicit): a candidate is the field's value stated directly under any wording that names the field (on the same line, or on the next line, or in a table cell) or in a clause; a field named but followed by something that is not a value is unparseable (as dev-u1); a sentence that only refers elsewhere or describes a formula is not a candidate (not found).

## Protocol
- heldout-2 is never used to write or tune a prompt, a model configuration, a normaliser or a rule. Prompt work uses the dev split only.
- The rules baseline (rules-1) is measured on heldout-2 in the PR that registers it (`heldout-2-baseline.json`), before any model exists.
- Before the first live heldout-2 run, the prompt, model ID and configuration are frozen and their hashes committed. The frozen configuration is measured once: three runs.
- A later change cannot be measured on heldout-2 again; it needs a new, fresh held-out split.

## Thresholds (fixed now)
Hard, on every run:
- all splits: schema-valid 100 %, citations resolving 100 %, 0 unsupported claims, failure codes exact;
- dev: every accuracy metric 1.0, 0 silent conflicts, 0 wrong values;
- heldout (first split): no regression against `heldout-baseline.json`.

Primary, heldout-2, on the worst of the three runs, against rules-1 on heldout-2:
- field accuracy at least +0.15 (about 17 of 112 fields);
- evidence completeness at least +0.15;
- abstention recall, conflict recall: not lower;
- wrong values (accepted values the expected output does not list, such as a planted value), silent conflicts: not higher.

Budget per document: p95 latency at most 20 s, mean cost at most USD 0.05.

Outcome accuracy is reported but is not a criterion: model-sourced fields are blocking by design, so assisted outcomes differ from the contract's labels.

**Decision rule:** model assistance may be enabled only if every hard gate holds on every run, every primary threshold holds on the worst run, and the budget holds. Otherwise the result is reported as measured, including a negative result, and nothing is tuned on heldout-2.

## Limitations
- 16 cases: directional evidence; every metric is reported with its counts.
- One author wrote the cases and will write the prompt; mitigated by predeclared categories and frozen expectations, not removed.
- Architecture B′ never consults the model for a field the rules found, so c1's conflict stays silent for both extractors; it is measured, not hidden.
