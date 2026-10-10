"""Container entrypoint: runs the parser as a child and reports a kernel OOM kill.

Docker's OOMKilled flag is set from an asynchronous event that is sometimes
never recorded (seen on Docker 28): an out-of-memory kill then looks like any
other SIGKILL. The kernel's own record is authoritative: the container's
cgroup v2 `memory.events` counts every OOM kill (`oom_kill`), and the container
can read it without any privilege (its cgroup namespace root).

The child is always the parser (`python -I -m docparser`), with this process's
stdin and stderr. Its stdout is buffered up to MAX_OUTPUT_BYTES and passed on
unchanged; beyond that it is streamed, so the runner's output cap applies as
before. How the child ended decides the exit:
- a normal exit (any status): its output and status, unchanged;
- killed by SIGKILL while `oom_kill` increased (read before the start and after
  the end): the parser's own failure result `memory_exceeded`, status 0;
- any other signal, or no readable increase: status 128 + signal, as if the
  parser itself had been the process, so the runner classifies it as before.
Nothing is ever inferred from SIGKILL alone, and nothing about the document is
written or logged.
"""

import json
import resource
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

PARSER = (sys.executable, "-I", "-m", "docparser")
MEMORY_EVENTS = Path("/sys/fs/cgroup/memory.events")
# Equal to the runner's default output cap (apps/api sandbox.SandboxSettings).
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_CHUNK = 64 * 1024


def main() -> int:
    return supervise(PARSER)


def supervise(
    command: Sequence[str],
    events: Path = MEMORY_EVENTS,
    stdout: BinaryIO | None = None,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> int:
    out = stdout if stdout is not None else sys.stdout.buffer
    before = oom_kills(events)
    buffered = bytearray()
    streaming = False
    with subprocess.Popen(command, stdout=subprocess.PIPE) as child:
        assert child.stdout is not None
        while chunk := child.stdout.read(_CHUNK):
            if streaming:
                out.write(chunk)
                continue
            buffered += chunk
            if len(buffered) > max_output_bytes:
                out.write(buffered)
                streaming = True
    status = child.returncode
    if status < 0 and -status == signal.SIGKILL and not streaming:
        after = oom_kills(events)
        if before is not None and after is not None and after > before:
            out.write(_memory_exceeded())
            out.flush()
            return 0
    if not streaming:
        out.write(buffered)
    out.flush()
    return status if status >= 0 else 128 - status


def oom_kills(events: Path) -> int | None:
    """The cgroup's oom_kill count, or None if it cannot be read exactly."""
    try:
        text = events.read_text(encoding="ascii")
    except OSError, UnicodeDecodeError:
        return None
    counts: dict[str, str] = {}
    for line in text.splitlines():
        name, _, value = line.partition(" ")
        counts[name] = value
    value = counts.get("oom_kill", "")
    return int(value) if value.isdecimal() else None


def _memory_exceeded() -> bytes:
    """The parser's own failure result, with the killed child's run metrics."""
    from docparser.output import failure  # imported only now: the child has ended

    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    result = failure("memory_exceeded")
    result["metrics"] = {
        "cpu_seconds": round(usage.ru_utime + usage.ru_stime, 3),
        "max_rss_kib": usage.ru_maxrss,
    }
    return json.dumps(result, ensure_ascii=False).encode("utf-8")


if __name__ == "__main__":
    sys.exit(main())
