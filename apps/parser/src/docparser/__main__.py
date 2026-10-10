"""Container entrypoint: PDF bytes on stdin -> one JSON document on stdout.

Exit status 0 means stdout holds a complete result: either the parsed pages or
a fixed failure code. Anything else (a signal, a crash, a non-zero status) is
treated by the runner as a failure without trusting stdout. Nothing about the
document is ever written to stderr or logged: pdfminer's logging and Python
warnings are silenced, and failures carry only a code.
"""

import json
import logging
import resource
import sys
import warnings

from docparser.limits import DEFAULT_LIMITS, Limits
from docparser.output import failure, success
from docparser.parse import ParseFailure, parse


def main(limits: Limits = DEFAULT_LIMITS) -> int:
    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")
    _set_rlimits(limits)
    data = sys.stdin.buffer.read(limits.max_input_bytes + 1)
    try:
        result = success(parse(data, limits))
    except ParseFailure as error:
        result = failure(error.code)
    except MemoryError:
        result = failure("memory_exceeded")
    result["metrics"] = _metrics()
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


def _set_rlimits(limits: Limits) -> None:
    """Second layer inside the container (the runner sets the container limits)."""
    # CPU: soft limit only, below the container's hard limit, so exceeding it
    # sends SIGXCPU (a timeout) rather than SIGKILL.
    _soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
    cpu = limits.cpu_seconds if hard == resource.RLIM_INFINITY else min(limits.cpu_seconds, hard)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, hard))
    for limit, value in (
        (resource.RLIMIT_AS, limits.max_address_space_bytes),
        (resource.RLIMIT_FSIZE, 0),
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_NOFILE, 16),
    ):
        _soft, hard = resource.getrlimit(limit)
        target = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(limit, (target, target))


def _metrics() -> dict[str, float | int]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    # ru_maxrss is in kibibytes on Linux (the container), bytes on macOS.
    return {
        "cpu_seconds": round(usage.ru_utime + usage.ru_stime, 3),
        "max_rss_kib": usage.ru_maxrss,
    }


if __name__ == "__main__":
    sys.exit(main())
