"""The in-container supervisor (host run, real child processes, a scripted memory.events)."""

import io
import json
import signal
import sys
from pathlib import Path

import pytest

from docparser import supervise
from docparser.supervise import oom_kills

EVENTS = "low 0\nhigh 0\nmax 3\noom {n}\noom_kill {n}\noom_group_kill 0\n"


def _events(tmp_path: Path, count: int = 0) -> Path:
    path = tmp_path / "memory.events"
    path.write_text(EVENTS.format(n=count))
    return path


def _child(code: str) -> list[str]:
    return [sys.executable, "-I", "-c", code]


def _kill_self(events: Path | None, signum: int = signal.SIGKILL) -> str:
    """A child that (optionally) raises the scripted oom_kill count, then dies by a signal."""
    bump = (
        f"p = {str(events)!r}; t = open(p).read(); open(p, 'w').write(t.replace("
        "'oom_kill 0', 'oom_kill 1')); "
        if events is not None
        else ""
    )
    return f"import os, sys; sys.stdout.write('partial'); sys.stdout.flush(); {bump}" + (
        f"os.kill(os.getpid(), {int(signum)})"
    )


def _run(command: list[str], events: Path, **kwargs: int) -> tuple[int, bytes]:
    out = io.BytesIO()
    status = supervise.supervise(command, events=events, stdout=out, **kwargs)
    return status, out.getvalue()


def test_a_normal_result_is_passed_on_byte_for_byte(tmp_path: Path) -> None:
    payload = '{"schema": "docparser.output.v1", "ok": true, "text": "Zo\u00eb \u2013 \u00fc"}'
    data = payload.encode()
    status, out = _run(_child(f"import sys; sys.stdout.buffer.write({data!r})"), _events(tmp_path))
    assert (status, out) == (0, data)


def test_a_non_zero_exit_keeps_its_status_and_output(tmp_path: Path) -> None:
    status, out = _run(_child("import sys; print('x'); sys.exit(3)"), _events(tmp_path))
    assert (status, out) == (3, b"x\n")


def test_a_kernel_oom_kill_is_memory_exceeded(tmp_path: Path) -> None:
    events = _events(tmp_path)
    status, out = _run(_child(_kill_self(events)), events)
    assert status == 0
    result = json.loads(out)
    assert result["schema"] == "docparser.output.v1"
    assert (result["ok"], result["code"]) == (False, "memory_exceeded")
    assert set(result["versions"]) == {"docparser", "pdfminer.six", "normalizer"}
    assert set(result["metrics"]) == {"cpu_seconds", "max_rss_kib"}
    assert b"partial" not in out  # the killed child's output is never passed on


def test_a_sigkill_without_an_oom_kill_is_not_memory_exceeded(tmp_path: Path) -> None:
    status, out = _run(_child(_kill_self(None)), _events(tmp_path))
    assert (status, out) == (128 + signal.SIGKILL, b"partial")


def test_another_signal_is_not_memory_exceeded_even_if_the_count_rose(tmp_path: Path) -> None:
    events = _events(tmp_path)
    status, out = _run(_child(_kill_self(events, signal.SIGTERM)), events)
    assert (status, out) == (128 + signal.SIGTERM, b"partial")


@pytest.mark.parametrize(
    "content",
    [None, "", "oom 1\n", "oom_kill\n", "oom_kill x\n", "oom_kill -1\n", "\xff"],
    ids=["missing", "empty", "no-oom-kill", "no-value", "not-a-number", "negative", "binary"],
)
def test_an_unreadable_counter_is_never_out_of_memory(tmp_path: Path, content: str | None) -> None:
    events = tmp_path / "memory.events"
    if content is not None:
        events.write_bytes(content.encode("latin-1"))
    assert oom_kills(events) is None
    status, out = _run(_child(_kill_self(None)), events)
    assert (status, out) == (128 + signal.SIGKILL, b"partial")


def test_a_counter_unreadable_only_after_the_kill_is_never_out_of_memory(tmp_path: Path) -> None:
    events = _events(tmp_path)
    code = f"import os; os.remove({str(events)!r}); os.kill(os.getpid(), 9)"
    status, out = _run(_child(code), events)
    assert (status, out) == (128 + signal.SIGKILL, b"")


def test_a_counter_unreadable_before_the_start_is_never_out_of_memory(tmp_path: Path) -> None:
    events = tmp_path / "memory.events"  # missing at the start, present (and raised) after
    write = f"open({str(events)!r}, 'w').write({EVENTS.format(n=1)!r})"
    code = f"{write}; import os; os.kill(os.getpid(), 9)"
    status, out = _run(_child(code), events)
    assert (status, out) == (128 + signal.SIGKILL, b"")


def test_output_over_the_cap_is_streamed_unchanged(tmp_path: Path) -> None:
    code = "import sys; sys.stdout.write('a' * 1000 + 'b' * 1000)"
    status, out = _run(_child(code), _events(tmp_path), max_output_bytes=1500)
    assert (status, out) == (0, b"a" * 1000 + b"b" * 1000)


def test_an_oom_kill_after_output_was_streamed_is_not_rewritten(tmp_path: Path) -> None:
    events = _events(tmp_path)
    code = "import sys; sys.stdout.write('a' * 2000); sys.stdout.flush(); " + _kill_self(events)
    status, out = _run(_child(code), events, max_output_bytes=1500)
    assert status == 128 + signal.SIGKILL
    assert out == b"a" * 2000 + b"partial"


def test_the_production_child_is_always_the_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def record(command: object) -> int:
        seen.append(command)
        return 0

    monkeypatch.setattr(supervise, "supervise", record)
    assert supervise.main() == 0
    assert seen == [(sys.executable, "-I", "-m", "docparser")]


def test_the_counter_is_read_from_the_container_cgroup() -> None:
    assert Path("/sys/fs/cgroup/memory.events") == supervise.MEMORY_EVENTS
    assert oom_kills(Path("/nonexistent/memory.events")) is None
