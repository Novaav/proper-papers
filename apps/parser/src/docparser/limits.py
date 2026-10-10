"""Resource and content limits for one parse (one document, one container).

The values are provisional measurement hypotheses (DI-1 plan, decision F8):
they were checked against the corpus and the measurement documents in
`scripts/measure_parser.py` and change only when measurements justify it. The
container limits (CPU, memory, processes, wall time) are set by the worker-side
runner (apps/api/src/api/sandbox.py) and repeated here only where the process
enforces them itself as a second layer.
"""

from dataclasses import dataclass

MIB = 1024 * 1024


@dataclass(frozen=True)
class Limits:
    # The upload limit (D-051); anything larger never reaches the parser.
    max_input_bytes: int = 20 * MIB
    max_pages: int = 100
    # Characters of normalized text.
    max_page_chars: int = 200_000
    max_total_chars: int = 1_000_000
    # Second layer below the container's memory limit, so an oversized
    # allocation surfaces as a controlled `memory_exceeded` where possible.
    max_address_space_bytes: int = 448 * MIB
    cpu_seconds: int = 30


DEFAULT_LIMITS = Limits()
