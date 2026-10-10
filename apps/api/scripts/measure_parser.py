"""Measure the parser sandbox against its provisional limits (DI-1, decision F8).

    make parser-measure  (PARSER_IMAGE=<image> uv run --locked python -m scripts.measure_parser)

Runs every corpus case and a set of stress documents through the real
sandbox (same flags as production use) and prints, per document: outcome,
wall time (including container start), parser CPU seconds and peak memory.
Stress documents are benign and synthetic: dense text near the page and
character limits, a 20 MiB file whose bytes are an image, and documents just
over each limit. Nothing is written or stored.
"""

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from api.sandbox import MAX_INPUT_BYTES, ParseSucceeded, SandboxSettings, parse_in_sandbox

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "corpus"))

from cases import CASES  # noqa: E402
from pdfgen import Document, Line, Page, build, text_page  # noqa: E402

WORDS = "provider customer effective agreement services governing notice value term fees "


def dense_page(chars: int) -> Page:
    """About `chars` characters (up to ~20 000) of 5 pt text that stays on an A4 page."""
    per_line, lines = 150, []
    text = WORDS * (chars // len(WORDS) + 1)
    for n in range(chars // per_line):
        lines.append(Line(text[n * per_line : (n + 1) * per_line], 830 - n * 6, x=15, size=5))
    return Page(tuple(lines))


def _pages(count: int, chars: int) -> Callable[[], bytes]:
    return lambda: build(Document(tuple(dense_page(chars) for _ in range(count))))


def stress() -> list[tuple[str, Callable[[], bytes]]]:
    def image_document() -> bytes:
        side = 4400  # 19.4 MB of image data, just under the 20 MiB upload limit
        pages = (Page(image=True), text_page("Provider: Example Ltd"))
        data = build(Document(pages, image_side=side))
        assert len(data) <= MAX_INPUT_BYTES, len(data)
        return data

    return [
        ("stress-100p-9900c-near-limits", _pages(100, 9_900)),
        ("stress-1p-20000c", _pages(1, 20_000)),
        ("stress-20mib-image", image_document),
        ("stress-101p-over-page-limit", _pages(101, 150)),
        ("stress-100p-11000c-over-total", _pages(100, 11_000)),
    ]


def main() -> int:
    image = os.environ.get("PARSER_IMAGE", "")
    if not image:
        print("set PARSER_IMAGE (make parser-image)", file=sys.stderr)
        return 2
    settings = SandboxSettings(image=image, docker=os.environ.get("PARSER_DOCKER", "docker"))
    documents = [(case.id, case.make) for case in CASES] + stress()
    print(f"{'document':34} {'bytes':>10} {'outcome':24} {'wall s':>7} {'cpu s':>6} {'rss MiB':>8}")
    for name, make in documents:
        data = make()
        started = time.monotonic()
        outcome = parse_in_sandbox(data, settings)
        wall = time.monotonic() - started
        if isinstance(outcome, ParseSucceeded):
            chars = sum(len(page.text) for page in outcome.pages)
            label = f"ok {len(outcome.pages)}p {chars}c"
            cpu = f"{outcome.metrics.cpu_seconds:.2f}"
            rss = f"{outcome.metrics.max_rss_kib / 1024:.0f}"
        else:
            label, cpu, rss = outcome.code, "-", "-"
        print(f"{name:34} {len(data):>10} {label:24} {wall:>7.2f} {cpu:>6} {rss:>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
