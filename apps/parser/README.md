# Parser

The isolated PDF text parser (DI-1). It turns the bytes of one PDF into normalized per-page text and a few content flags, or a fixed failure code. It runs only inside its container, one container per document, started by the worker-side runner in `apps/api/src/api/sandbox.py`, which the processing worker (`apps/api/src/api/worker.py`) calls for each claimed run.

## What it does

- **Input:** the PDF bytes on stdin (at most 20 MiB).
- **Output:** one JSON document on stdout (schema `docparser.output.v1`).
  - Either `ok: true` with `pages` (number, normalized text, and counts of excluded or removed characters) and `features` (`javascript`, `xfa`),
  - or `ok: false` with a `code`.
  - Both carry the parser, pdfminer.six and normalizer versions, plus run metrics (CPU seconds, peak memory).
- **Library:** [pdfminer.six](https://github.com/pdfminer/pdfminer.six) `20260107` (MIT), pinned exactly.
  - It's pure Python, so the parser itself has no memory-corruption class of bugs.
  - It pulls in `cryptography` and `charset-normalizer`.
  - The release includes the fixes for the two 2025 CMap/pickle advisories. Unreleased upstream fixes for resource exhaustion (a PNG predictor running out of memory, circular cross-reference tables) are contained by the sandbox limits.

Only the page text layer is read:
- **Text removed or counted:**
  - Text outside the page box, and text in the invisible rendering modes (3, 7), is excluded and counted.
  - Undefined glyphs become U+FFFD and are counted.
  - Zero-width, bidi and other format or control characters are removed and counted.
- **Normalization** (`normalize.py`, version 1): NFKC, unified line breaks and spaces, trimmed. It's deterministic, and evidence offsets will refer to this text.
- **Refused, never partially processed:**
  - embedded files and file attachments, and rich media (`embedded_content`, decision F4);
  - declared encryption (`encrypted_pdf`, as at upload);
  - documents whose cross-reference table would need repair (`parse_failed`, strict parsing).
- **JavaScript and XFA** are never executed; they're only flagged.
- **Limits** (`limits.py`, provisional):
  - 100 pages (`too_many_pages`);
  - 200 000 characters per page and 1 000 000 in total (`text_too_large`);
  - no text at all gives `no_text_layer` (no OCR).

White or tiny text, text hidden behind images, and clipping are **not** detected. It is extracted like any other text.

## Isolation (enforced by the runner; verified by `apps/api/tests/test_parser_sandbox.py`)

- **The container runs with:**
  - `--network none` (only loopback is up, and there are no routes);
  - `--read-only` with no mounts or temporary filesystem;
  - `--cap-drop ALL`, `no-new-privileges` and user 65534;
  - `--ipc none` and `--init` (the parser is not PID 1, so signals and limits act normally).
- **Container limits:**
  - 512 MiB memory without swap;
  - 1 CPU and 32 processes;
  - CPU time 30 s soft and 31 s hard (reported as `timeout`);
  - a file size of 0, no core dumps, 64 open files;
  - `--log-driver none`, so Docker keeps no copy of the output;
  - `--pull never`: only a locally built image is used.
- **Second layer, inside the container** (`__main__.py`): address space 448 MiB (a huge allocation becomes `memory_exceeded`), CPU soft limit 30 s, 16 open files, file size 0.
- **Supervisor** (`supervise.py`, the image's entrypoint): runs the parser as its only child. If the kernel kills it for memory (SIGKILL while the container's cgroup v2 `memory.events` `oom_kill` count rises), it reports the parser's own `memory_exceeded` result; Docker's `OOMKilled` flag is sometimes never recorded (seen on Docker 28). Any other ending is passed on unchanged (output, or exit status 128 + signal), and an unreadable counter never counts as out of memory. It holds at most 8 MiB of output and uses about 15–23 MiB, within the 64 MiB between the parser's 448 MiB address space and the 512 MiB container limit.
- **Runner side:**
  - a 60 s wall clock (it kills the container);
  - an 8 MiB stdout cap;
  - strict validation of the output;
  - stderr is discarded and never logged.
  - A non-zero exit or a signal is `parse_failed`, and an unavailable Docker or missing image is `sandbox_unavailable`. That is the only transient failure, and there is never an in-process fallback: the API has no PDF library.
- **Image:** `python:3.14-slim` pinned by its index digest; hash-locked wheel-only dependencies (`requirements.txt`, exported from `uv.lock`); pip removed; source compiled at build time.

The Docker CLI runner is for development and CI. A deployment must provide the same isolation without giving the worker the Docker socket ([`docs/deployment-security.md`](../../docs/deployment-security.md), section 2).

## Corpus

`corpus/` at the repository root holds the synthetic DI-1 corpus:
- a standard-library PDF writer (`pdfgen.py`) and 30 cases (`cases.py`): 22 `dev` and 8 `heldout`, covering normal, duplicate, missing, ambiguous, unparseable, conflict, malformed and adversarial documents, all fictional and benign;
- `manifest.json`, which pins every case's SHA-256;
- `expected/parse/`, the parse expectations (`expected/extract/` and the held-out baseline are described in `corpus/README.md`).

The PDFs are generated byte-identically, so they aren't committed. `heldout` cases must never be used to tune an extractor.

## Commands

```sh
make parser-setup     # locked environment for the host tests
make parser-check     # ruff, mypy, pytest (corpus expectations), corpus manifest, requirements sync
make parser-image     # build production-ai-flagship-parser:local
PARSER_IMAGE=production-ai-flagship-parser:local make api-check   # includes the real sandbox tests
make parser-measure   # limits measurement through the real sandbox (report only)
```

After a deliberate parser or corpus change: `python3 corpus/generate.py manifest` and `uv run --locked python scripts/write_parse_goldens.py` (from `apps/parser`), then review the diffs.
