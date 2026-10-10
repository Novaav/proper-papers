"""Worker-side runner for the isolated PDF parser (DI-1): one container per document.

The only way document bytes are parsed. Each call starts one short-lived
container from the parser image (apps/parser) with no network, a read-only
root filesystem, no capabilities, no privilege escalation, an unprivileged
user and CPU, memory, process, file and wall-clock limits; the bytes go in on
stdin and one JSON result comes back on stdout. There is no fallback: if the
container cannot run, the result is `sandbox_unavailable`, never parsing in
this process (this package does not depend on any PDF library).

Callers pass only bytes that the database lets them read, which for document
content means the latest malware verdict is clean (migration 0007). A clean
verdict is a precondition, not proof of safety: the container treats every
document as hostile.

Everything the container prints is untrusted. stdout is size-capped and
validated against a strict schema (anything else is `output_invalid`); stderr
is drained and discarded, never logged, because it could contain document
text. Logs carry only the fixed failure code.
"""

import json
import logging
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import IO, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger(__name__)

MIB = 1024 * 1024
# Limits (DI-1 plan, decision F8: provisional, measured in apps/api/scripts/measure_parser.py).
MAX_INPUT_BYTES = 20 * MIB  # the upload limit (D-051)
MAX_PAGES = 100
MAX_PAGE_CHARS = 200_000
MAX_TOTAL_CHARS = 1_000_000

# Codes the parser itself reports, and those the runner adds.
PARSER_FAILURES = frozenset(
    {
        "input_too_large",
        "parse_failed",
        "encrypted_pdf",
        "embedded_content",
        "too_many_pages",
        "text_too_large",
        "no_text_layer",
        "memory_exceeded",
    }
)
RUNNER_FAILURES = frozenset(
    {"timeout", "memory_exceeded", "output_too_large", "output_invalid", "sandbox_unavailable"}
)
# Only an unavailable sandbox is worth retrying: every other outcome is a
# deterministic function of the same bytes and the same parser image.
TRANSIENT_FAILURES = frozenset({"sandbox_unavailable"})

RUN_LABEL = "org.production-ai-flagship.role=parser-run"
# Exit status of a process killed by SIGXCPU (the CPU-time limit): 128 + 24.
EXIT_CPU_LIMIT = 152
# Exit status of a process killed by SIGKILL (128 + 9), which is how the kernel's
# out-of-memory killer ends the parser. Docker records an out-of-memory kill
# (State.OOMKilled) from a separate, asynchronous event that can be processed
# after the exit (moby v28: daemon/monitor.go handles EventOOM on its own;
# State.SetStopped leaves OOMKilled untouched), so an immediate inspect can miss
# it. A SIGKILL exit without the flag is therefore checked again, for a bounded
# time, before it counts as an ordinary crash. Nothing is ever classified as
# out of memory from the exit status alone.
EXIT_SIGKILL = 137
OOM_FLAG_WAIT_SECONDS = 5.0
OOM_FLAG_POLL_SECONDS = 0.1
STDERR_DRAIN_BYTES = 64 * 1024
DOCKER_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class SandboxSettings:
    image: str  # a locally built parser image; never pulled (`--pull never`)
    docker: str = "docker"
    wall_seconds: float = 60.0
    cpu_seconds: int = 30
    memory: str = "512m"
    cpus: str = "1"
    pids: int = 32
    max_output_bytes: int = 8 * MIB


def docker_run_command(
    settings: SandboxSettings, name: str, entrypoint: Sequence[str] = ()
) -> list[str]:
    """The exact `docker run` invocation; `entrypoint` replaces the parser (tests only)."""
    command = [
        settings.docker,
        "run",
        "--name",
        name,
        "--label",
        RUN_LABEL,
        "--interactive",
        # A minimal init as PID 1: the kernel ignores default-action signals
        # (SIGXCPU, SIGSEGV) sent to PID 1, so the parser must not be PID 1.
        "--init",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "65534:65534",
        "--ipc",
        "none",
        "--pids-limit",
        str(settings.pids),
        "--memory",
        settings.memory,
        "--memory-swap",
        settings.memory,
        "--cpus",
        settings.cpus,
        "--ulimit",
        # Soft below hard: the soft limit sends SIGXCPU (reported as a timeout); an
        # equal hard limit would send SIGKILL instead, indistinguishable from a crash.
        f"cpu={settings.cpu_seconds}:{settings.cpu_seconds + 1}",
        "--ulimit",
        "fsize=0:0",
        "--ulimit",
        "core=0:0",
        "--ulimit",
        "nofile=64:64",
        # Docker would otherwise keep the container's output (document text) in log files.
        "--log-driver",
        "none",
    ]
    if entrypoint:
        command += ["--entrypoint", entrypoint[0], settings.image, *entrypoint[1:]]
    else:
        command.append(settings.image)
    return command


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Versions(_Strict):
    docparser: Annotated[str, Field(min_length=1, max_length=40)]
    pdfminer_six: Annotated[str, Field(min_length=1, max_length=40, alias="pdfminer.six")]
    normalizer: Annotated[str, Field(min_length=1, max_length=40)]


class Metrics(_Strict):
    cpu_seconds: Annotated[float, Field(ge=0)] | Annotated[int, Field(ge=0)]
    max_rss_kib: Annotated[int, Field(ge=0)]


class ParsedPage(_Strict):
    number: Annotated[int, Field(ge=1, le=MAX_PAGES)]
    text: Annotated[str, Field(max_length=MAX_PAGE_CHARS)]
    off_page_chars: Annotated[int, Field(ge=0)]
    invisible_chars: Annotated[int, Field(ge=0)]
    undefined_chars: Annotated[int, Field(ge=0)]
    control_chars_removed: Annotated[int, Field(ge=0)]


class Features(_Strict):
    javascript: bool
    xfa: bool


class _Success(_Strict):
    schema_: Literal["docparser.output.v1"] = Field(alias="schema")
    ok: Literal[True]
    versions: Versions
    page_count: Annotated[int, Field(ge=1, le=MAX_PAGES)]
    pages: Annotated[list[ParsedPage], Field(min_length=1, max_length=MAX_PAGES)]
    features: Features
    metrics: Metrics


class _Failure(_Strict):
    schema_: Literal["docparser.output.v1"] = Field(alias="schema")
    ok: Literal[False]
    versions: Versions
    code: str
    metrics: Metrics


@dataclass(frozen=True)
class ParseSucceeded:
    pages: tuple[ParsedPage, ...]
    features: Features
    versions: Versions
    metrics: Metrics


@dataclass(frozen=True)
class ParseFailed:
    code: str

    @property
    def transient(self) -> bool:
        return self.code in TRANSIENT_FAILURES


ParseOutcome = ParseSucceeded | ParseFailed


def parse_in_sandbox(content: bytes, settings: SandboxSettings) -> ParseOutcome:
    """Parse one document in a fresh isolated container; never raises for document problems."""
    if len(content) > MAX_INPUT_BYTES:
        return _failed("input_too_large")
    name = f"production-ai-flagship-parse-{uuid.uuid4().hex}"
    try:
        return _run(content, settings, name)
    finally:
        _remove(settings, name)


def _run(content: bytes, settings: SandboxSettings, name: str) -> ParseOutcome:
    try:
        process = subprocess.Popen(
            docker_run_command(settings, name),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        return _failed("sandbox_unavailable")
    with process:
        assert process.stdin and process.stdout and process.stderr
        stdout = _CappedReader(process.stdout, settings.max_output_bytes, on_overflow=process.kill)
        threads = [
            threading.Thread(target=_feed, args=(process.stdin, content), daemon=True),
            threading.Thread(target=stdout.run, daemon=True),
            threading.Thread(target=_drain, args=(process.stderr,), daemon=True),
        ]
        for thread in threads:
            thread.start()
        timed_out = False
        try:
            process.wait(timeout=settings.wall_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Killing the client does not stop the container: kill the container itself.
            _docker(settings, "kill", name)
            process.kill()
            process.wait()
        for thread in threads:
            thread.join(timeout=DOCKER_TIMEOUT_SECONDS)

    if stdout.overflowed:
        _docker(settings, "kill", name)
        return _failed("output_too_large")
    if timed_out:
        return _failed("timeout")
    state = _state(settings, name)
    if state is None:
        return _failed("sandbox_unavailable")  # the container was never created
    code = _exit_failure(settings, name, *state)
    return _failed(code) if code else _interpret(stdout.data)


def _exit_failure(
    settings: SandboxSettings, name: str, oom_killed: bool, exit_code: int
) -> str | None:
    """The failure code for how the container ended, or None for a normal exit."""
    if not oom_killed and exit_code == EXIT_SIGKILL:
        oom_killed = _await_oom_flag(settings, name)
    if oom_killed:
        return "memory_exceeded"
    if exit_code == EXIT_CPU_LIMIT:
        return "timeout"
    if exit_code != 0:
        log.info("parser container exited with status %d", exit_code)
        return "parse_failed"  # crashed or killed: stdout is not trusted
    return None


def _await_oom_flag(settings: SandboxSettings, name: str) -> bool:
    """Whether Docker records an out-of-memory kill within OOM_FLAG_WAIT_SECONDS.

    Both outcomes are logged (counts and seconds only): a late flag at INFO, and a
    SIGKILL that never gets one, classified parse_failed, at WARNING.
    """
    started = time.monotonic()
    deadline = started + OOM_FLAG_WAIT_SECONDS
    polls = failed_inspects = 0
    while time.monotonic() < deadline:
        time.sleep(OOM_FLAG_POLL_SECONDS)
        state = _state(settings, name)
        polls += 1
        failed_inspects += state is None
        if state is not None and state[0]:
            log.info(
                "out-of-memory flag recorded %.2f s after the SIGKILL exit (poll %d)",
                time.monotonic() - started,
                polls,
            )
            return True
    log.warning(
        "SIGKILL exit without an out-of-memory record after %.2f s "
        "(%d polls, %d failed inspects); classified parse_failed",
        time.monotonic() - started,
        polls,
        failed_inspects,
    )
    return False


def _interpret(data: bytes) -> ParseOutcome:
    try:
        document = json.loads(data)
        if not isinstance(document, dict):
            return _failed("output_invalid")
        if document.get("ok") is True:
            success = _Success.model_validate(document)
        else:
            failure = _Failure.model_validate(document)
            return _failed(failure.code if failure.code in PARSER_FAILURES else "output_invalid")
    # RecursionError: hostile, deeply nested JSON from a compromised parser.
    except ValueError, ValidationError, RecursionError:
        return _failed("output_invalid")
    numbers = [page.number for page in success.pages]
    total = sum(len(page.text) for page in success.pages)
    if numbers != list(range(1, len(numbers) + 1)) or success.page_count != len(numbers):
        return _failed("output_invalid")
    if total > MAX_TOTAL_CHARS or total == 0:
        return _failed("output_invalid")
    return ParseSucceeded(
        pages=tuple(success.pages),
        features=success.features,
        versions=success.versions,
        metrics=success.metrics,
    )


def _failed(code: str) -> ParseFailed:
    assert code in PARSER_FAILURES | RUNNER_FAILURES, code
    log.info("parse refused: %s", code)
    return ParseFailed(code)


class _CappedReader:
    def __init__(self, stream: IO[bytes], limit: int, on_overflow: Callable[[], None]) -> None:
        self._stream = stream
        self._limit = limit
        self._on_overflow = on_overflow
        self._chunks: list[bytes] = []
        self._size = 0
        self.overflowed = False

    def run(self) -> None:
        while chunk := self._stream.read(64 * 1024):
            self._size += len(chunk)
            if self._size > self._limit:
                self.overflowed = True
                self._chunks.clear()
                self._on_overflow()
                return
            self._chunks.append(chunk)

    @property
    def data(self) -> bytes:
        return b"".join(self._chunks)


def _feed(stdin: IO[bytes], content: bytes) -> None:
    try:
        stdin.write(content)
        stdin.close()
    except OSError:
        pass  # the container exited or was killed before reading everything


def _drain(stderr: IO[bytes]) -> None:
    # Read and discard: never logged or stored (it could contain document text).
    remaining = STDERR_DRAIN_BYTES
    while remaining > 0 and (chunk := stderr.read(min(remaining, 8192))):
        remaining -= len(chunk)
    stderr.close()


def image_id(settings: SandboxSettings) -> str | None:
    """The full id (`sha256:...`) of the local image `settings.image` names; None if absent.

    The worker compares it with PARSER_IMAGE_ID before it starts, so the pipeline
    version names the exact parser artifact (DI-1 PR 3). Never pulls.
    """
    result = _docker(settings, "image", "inspect", "--format", "{{.Id}}", settings.image)
    if result is None or result.returncode != 0:
        return None
    value = result.stdout.decode("ascii", "replace").strip()
    return value or None


def _state(settings: SandboxSettings, name: str) -> tuple[bool, int] | None:
    result = _docker(
        settings, "inspect", "--format", "{{.State.OOMKilled}} {{.State.ExitCode}}", name
    )
    if result is None or result.returncode != 0:
        return None
    oom, _, code = result.stdout.decode("ascii", "replace").strip().partition(" ")
    try:
        return oom == "true", int(code)
    except ValueError:
        return None


def _remove(settings: SandboxSettings, name: str) -> None:
    result = _docker(settings, "rm", "--force", "--volumes", name)
    if result is None or result.returncode not in (0, 1):
        log.warning("parser container %s could not be removed", name)


def _docker(settings: SandboxSettings, *args: str) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            [settings.docker, *args],
            capture_output=True,
            timeout=DOCKER_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
