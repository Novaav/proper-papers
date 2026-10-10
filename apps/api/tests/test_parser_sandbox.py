"""The isolated parser runner (DI-1): isolation properties, limits and fail-closed outcomes.

Two groups:
- Unit tests of the runner's own logic (command, output validation, no Docker).
- Real-sandbox tests against the parser image (apps/parser): the whole corpus
  through the real container, and probes run in the same container with the
  same flags (only the entrypoint replaced) that check the isolation itself.
  They need PARSER_IMAGE (and optionally PARSER_DOCKER). Under GitHub Actions,
  or with REQUIRE_PARSER_SANDBOX=1, a missing image fails them instead of
  skipping them; elsewhere they are skipped unless PARSER_IMAGE is set.
"""

import importlib.util
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from api import sandbox
from api.sandbox import (
    MAX_INPUT_BYTES,
    RUN_LABEL,
    ParseFailed,
    ParseSucceeded,
    SandboxSettings,
    docker_run_command,
    parse_in_sandbox,
)

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "corpus"))

from cases import CASES, LABELLED, Case  # noqa: E402
from pdfgen import Document, build  # noqa: E402

EXPECTED = ROOT / "corpus" / "expected" / "parse"


def sandbox_required(environ: Mapping[str, str]) -> bool:
    return environ.get("GITHUB_ACTIONS") == "true" or environ.get("REQUIRE_PARSER_SANDBOX") == "1"


IMAGE = os.environ.get("PARSER_IMAGE", "")
DOCKER = os.environ.get("PARSER_DOCKER", "docker")
real = pytest.mark.skipif(
    not IMAGE and not sandbox_required(os.environ),
    reason="set PARSER_IMAGE (make parser-image) to run the real sandbox tests",
)


@pytest.fixture
def settings() -> SandboxSettings:
    if not IMAGE:
        pytest.fail("PARSER_IMAGE is not set; the real parser sandbox is mandatory here")
    return SandboxSettings(image=IMAGE, docker=DOCKER)


def _as_expectation(outcome: ParseSucceeded | ParseFailed) -> dict[str, Any]:
    """The runner's result in the shape of corpus/expected/parse/*.json."""
    if isinstance(outcome, ParseFailed):
        return {"ok": False, "code": outcome.code, "versions": None}
    return {
        "ok": True,
        "versions": outcome.versions.model_dump(by_alias=True),
        "page_count": len(outcome.pages),
        "pages": [page.model_dump() for page in outcome.pages],
        "features": outcome.features.model_dump(),
    }


def _with_entrypoint(monkeypatch: pytest.MonkeyPatch, entrypoint: Sequence[str]) -> None:
    original = sandbox.docker_run_command
    monkeypatch.setattr(
        sandbox,
        "docker_run_command",
        lambda settings, name: original(settings, name, entrypoint=entrypoint),
    )


def _python(code: str) -> list[str]:
    return ["python", "-I", "-c", code]


def _supervised(code: str) -> list[str]:
    """The image's supervisor (production path) with a test child instead of the parser."""
    child = f"[sys.executable, '-I', '-c', {code!r}]"
    return _python(
        f"import sys; from docparser.supervise import supervise; sys.exit(supervise({child}))"
    )


def _leftover_containers() -> list[str]:
    result = subprocess.run(
        [DOCKER, "ps", "--all", "--quiet", "--filter", f"label={RUN_LABEL}"],
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().split()


# ---------------------------------------------------------------- unit tests (no Docker)


def test_the_api_cannot_parse_pdfs_in_process() -> None:
    """No fallback is possible: the API environment has no PDF library at all."""
    for module in ("pdfminer", "pypdf", "fitz", "pypdfium2", "docparser"):
        assert importlib.util.find_spec(module) is None, module


def test_command_isolates_the_container() -> None:
    command = docker_run_command(SandboxSettings(image="parser:test"), "n")
    joined = " ".join(command)
    for flag in (
        "--init",
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--user 65534:65534",
        "--ipc none",
        "--pids-limit 32",
        "--memory 512m",
        "--memory-swap 512m",
        "--cpus 1",
        "--ulimit cpu=30:31",
        "--ulimit fsize=0:0",
        "--ulimit core=0:0",
        "--ulimit nofile=64:64",
        "--log-driver none",
        "--pull never",
        f"--label {RUN_LABEL}",
    ):
        assert flag in joined, flag
    assert command[-1] == "parser:test"
    for forbidden in ("--privileged", "--volume", "-v", "--mount", "--env", "-e", "--cap-add"):
        assert forbidden not in command
    assert "--rm" not in command  # removed explicitly after the exit state is read


def test_oversized_input_is_refused_before_starting_a_container() -> None:
    settings = SandboxSettings(image="parser:test", docker="/nonexistent/docker")
    assert parse_in_sandbox(b"%" * (MAX_INPUT_BYTES + 1), settings) == ParseFailed(
        "input_too_large"
    )


def test_missing_docker_fails_closed_as_unavailable() -> None:
    settings = SandboxSettings(image="parser:test", docker="/nonexistent/docker")
    outcome = parse_in_sandbox(build(Document((LABELLED,))), settings)
    assert outcome == ParseFailed("sandbox_unavailable")
    assert isinstance(outcome, ParseFailed) and outcome.transient


def test_only_unavailability_is_transient() -> None:
    assert [
        c for c in sandbox.PARSER_FAILURES | sandbox.RUNNER_FAILURES if ParseFailed(c).transient
    ] == ["sandbox_unavailable"]


def _success(**changes: object) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema": "docparser.output.v1",
        "ok": True,
        "versions": {"docparser": "0.1.0", "pdfminer.six": "20260107", "normalizer": "1"},
        "page_count": 1,
        "pages": [
            {
                "number": 1,
                "text": "Provider: Example Ltd",
                "off_page_chars": 0,
                "invisible_chars": 0,
                "undefined_chars": 0,
                "control_chars_removed": 0,
            }
        ],
        "features": {"javascript": False, "xfa": False},
        "metrics": {"cpu_seconds": 0.1, "max_rss_kib": 1000},
    }
    document.update(changes)
    return document


def _interpret(document: object) -> ParseSucceeded | ParseFailed:
    return sandbox._interpret(json.dumps(document).encode())


def test_valid_output_is_accepted() -> None:
    outcome = _interpret(_success())
    assert isinstance(outcome, ParseSucceeded)
    assert outcome.pages[0].text == "Provider: Example Ltd"


@pytest.mark.parametrize(
    "document",
    [
        "not an object",
        [],
        _success(extra="field"),
        _success(schema="docparser.output.v2"),
        _success(page_count=2),
        _success(pages=[]),
        _success(pages=[{**_success()["pages"][0], "number": 2}]),
        _success(pages=[{**_success()["pages"][0], "text": 5}]),
        _success(pages=[{**_success()["pages"][0], "text": ""}]),
        _success(pages=[{**_success()["pages"][0], "text": "x" * 200_001}]),
        _success(pages=[{**_success()["pages"][0], "off_page_chars": -1}]),
        _success(features={"javascript": "no", "xfa": False}),
        _success(ok="true"),
        {
            "schema": "docparser.output.v1",
            "ok": False,
            "code": "made_up",
            "versions": _success()["versions"],
            "metrics": _success()["metrics"],
        },
        {
            "schema": "docparser.output.v1",
            "ok": False,
            "code": "timeout",
            "versions": _success()["versions"],
            "metrics": _success()["metrics"],
        },
    ],
)
def test_invalid_output_is_refused(document: object) -> None:
    assert _interpret(document) == ParseFailed("output_invalid")


def test_total_text_limit_is_enforced_by_the_runner() -> None:
    page = _success()["pages"][0]
    pages = [{**page, "number": n, "text": "x" * 200_000} for n in range(1, 6)]
    assert isinstance(_interpret(_success(pages=pages, page_count=5)), ParseSucceeded)
    pages.append({**page, "number": 6, "text": "x"})
    assert _interpret(_success(pages=pages, page_count=6)) == ParseFailed("output_invalid")


def test_non_json_output_is_refused() -> None:
    assert sandbox._interpret(b"\xff not json") == ParseFailed("output_invalid")


@pytest.mark.parametrize(
    "data",
    [
        b"[" * 200_000 + b"]" * 200_000,
        b'{"ok": true, "pages": ' + b"[" * 200_000 + b"]" * 200_000 + b"}",
        b'{"ok": false, "code": ' + b'{"a": ' * 200_000 + b"1" + b"}" * 200_001,
    ],
    ids=["array", "inside-success", "inside-failure"],
)
def test_deeply_nested_output_is_refused_not_raised(data: bytes) -> None:
    """Hostile nesting exceeds the JSON decoder's recursion limit: it must not escape."""
    assert sandbox._interpret(data) == ParseFailed("output_invalid")


def test_a_reported_parser_failure_is_passed_through() -> None:
    document = {
        "schema": "docparser.output.v1",
        "ok": False,
        "code": "embedded_content",
        "versions": _success()["versions"],
        "metrics": _success()["metrics"],
    }
    assert _interpret(document) == ParseFailed("embedded_content")


def _scripted_inspect(
    monkeypatch: pytest.MonkeyPatch, flags: list[bool], exit_code: int = sandbox.EXIT_SIGKILL
) -> list[int]:
    """Replace `docker inspect` with a script of OOMKilled values; returns the call log."""
    calls: list[int] = []

    def state(_settings: SandboxSettings, _name: str) -> tuple[bool, int]:
        calls.append(len(calls))
        return (flags[min(len(calls) - 1, len(flags) - 1)], exit_code)

    monkeypatch.setattr(sandbox, "_state", state)
    monkeypatch.setattr(sandbox, "OOM_FLAG_POLL_SECONDS", 0.01)
    monkeypatch.setattr(sandbox, "OOM_FLAG_WAIT_SECONDS", 0.5)
    return calls


UNIT = SandboxSettings(image="parser:test", docker="/nonexistent/docker")


def _sandbox_records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "api.sandbox" and r.levelno == level]


def _counts(record: logging.LogRecord) -> list[int]:
    """The record's integer arguments (poll and failed-inspect counts), in order."""
    assert isinstance(record.args, tuple)
    return [arg for arg in record.args if isinstance(arg, int) and not isinstance(arg, bool)]


def test_an_out_of_memory_kill_recorded_late_by_docker_is_still_memory_exceeded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Docker sets OOMKilled from an event it may process after the exit.
    caplog.set_level(logging.INFO, logger="api.sandbox")
    calls = _scripted_inspect(monkeypatch, [False, False, True])
    assert sandbox._exit_failure(UNIT, "n", False, sandbox.EXIT_SIGKILL) == "memory_exceeded"
    assert len(calls) == 3
    [record] = _sandbox_records(caplog, logging.INFO)
    assert "out-of-memory flag recorded" in record.getMessage()
    assert _counts(record) == [3]  # the poll that saw the flag
    assert _sandbox_records(caplog, logging.WARNING) == []


def test_a_sigkill_without_an_out_of_memory_record_stays_parse_failed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="api.sandbox")
    calls = _scripted_inspect(monkeypatch, [False])
    started = time.monotonic()
    assert sandbox._exit_failure(UNIT, "n", False, sandbox.EXIT_SIGKILL) == "parse_failed"
    assert time.monotonic() - started >= 0.5  # waited the whole bound, then gave up
    assert len(calls) >= 2
    [record] = _sandbox_records(caplog, logging.WARNING)
    message = record.getMessage()
    assert "without an out-of-memory record" in message and "classified parse_failed" in message
    assert _counts(record) == [len(calls), 0]  # polls, failed inspects


def test_failed_inspects_during_the_wait_are_counted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="api.sandbox")
    _scripted_inspect(monkeypatch, [False])
    monkeypatch.setattr(sandbox, "_state", lambda _settings, _name: None)
    assert sandbox._exit_failure(UNIT, "n", False, sandbox.EXIT_SIGKILL) == "parse_failed"
    [record] = _sandbox_records(caplog, logging.WARNING)
    polls, failed_inspects = _counts(record)
    assert polls >= 1 and failed_inspects == polls


@pytest.mark.parametrize(
    ("oom_killed", "exit_code", "expected"),
    [
        (True, sandbox.EXIT_SIGKILL, "memory_exceeded"),
        (True, 1, "memory_exceeded"),
        (False, sandbox.EXIT_CPU_LIMIT, "timeout"),
        (False, 139, "parse_failed"),  # SIGSEGV
        (False, 1, "parse_failed"),
        (False, 143, "parse_failed"),  # SIGTERM
        (False, 0, None),
    ],
)
def test_other_exits_are_classified_at_once_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    oom_killed: bool,
    exit_code: int,
    expected: str | None,
) -> None:
    caplog.set_level(logging.INFO, logger="api.sandbox")
    calls = _scripted_inspect(monkeypatch, [True], exit_code)
    assert sandbox._exit_failure(UNIT, "n", oom_killed, exit_code) == expected
    assert calls == []  # only a SIGKILL without the flag is checked again
    assert _sandbox_records(caplog, logging.WARNING) == []


# ---------------------------------------------------------------- real sandbox


@real
@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_corpus_case_through_the_real_sandbox(case: Case, settings: SandboxSettings) -> None:
    expected = json.loads((EXPECTED / f"{case.id}.json").read_text())
    outcome = _as_expectation(parse_in_sandbox(case.make(), settings))
    if expected["ok"]:
        del expected["schema"]
        assert outcome == expected
    else:
        assert (outcome["ok"], outcome["code"]) == (False, expected["code"])


@real
def test_no_container_is_left_behind(settings: SandboxSettings) -> None:
    parse_in_sandbox(build(Document((LABELLED,))), settings)
    assert _leftover_containers() == []


PROBE = r"""
import json, os, resource, socket
def attempt(action):
    try:
        action()
        return "allowed"
    except OSError as error:
        return type(error).__name__
def write(path):
    with open(path, "wb") as f:
        f.write(b"x")
def connect(address):
    socket.create_connection(address, timeout=2).close()
lines = open("/proc/self/status").read().splitlines()
status = dict(line.split(":\t", 1) for line in lines if ":\t" in line)
def up(name):
    return int(open("/sys/class/net/" + name + "/flags").read(), 16) & 1 == 1
names = [line.split(":")[0].strip() for line in open("/proc/net/dev").read().splitlines()[2:]]
def cgroup(name):
    try:
        return open("/sys/fs/cgroup/" + name).read().strip()
    except OSError:
        return None
print(json.dumps({
    "uid": os.getuid(), "gid": os.getgid(), "euid": os.geteuid(),
    "cap_eff": status["CapEff"].strip(), "cap_bnd": status["CapBnd"].strip(),
    "no_new_privs": status["NoNewPrivs"].strip(),
    "interfaces_up": sorted(name for name in names if up(name)),
    "ipv4_routes": open("/proc/net/route").read().splitlines()[1:],
    "connect_ip": attempt(lambda: connect(("1.1.1.1", 53))),
    "resolve": attempt(lambda: socket.getaddrinfo("example.com", 443)),
    "write_root": attempt(lambda: write("/probe")),
    "write_tmp": attempt(lambda: write("/tmp/probe")),
    "write_site": attempt(lambda: write("/usr/local/lib/python3.14/site-packages/probe")),
    "rlimit_cpu": resource.getrlimit(resource.RLIMIT_CPU),
    "rlimit_fsize": resource.getrlimit(resource.RLIMIT_FSIZE),
    "rlimit_nofile": resource.getrlimit(resource.RLIMIT_NOFILE),
    "rlimit_core": resource.getrlimit(resource.RLIMIT_CORE),
    "memory_max": cgroup("memory.max"), "pids_max": cgroup("pids.max"),
    "env": sorted(os.environ),
}))
"""


@real
def test_isolation_properties_inside_the_container(
    settings: SandboxSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A secret in the runner's environment must not reach the container.
    monkeypatch.setenv("PARSER_SANDBOX_TEST_SECRET", "do-not-leak")
    name = f"production-ai-flagship-probe-{uuid.uuid4().hex}"
    try:
        result = subprocess.run(
            docker_run_command(settings, name, entrypoint=_python(PROBE)),
            input=b"",
            capture_output=True,
            timeout=120,
            check=True,
        )
    finally:
        subprocess.run([DOCKER, "rm", "--force", name], capture_output=True, check=False)
    probe = json.loads(result.stdout)
    assert (probe["uid"], probe["gid"], probe["euid"]) == (65534, 65534, 65534)
    assert int(probe["cap_eff"], 16) == 0
    assert int(probe["cap_bnd"], 16) == 0
    assert probe["no_new_privs"] == "1"
    # A --network none namespace may list the kernel's inactive tunnel devices;
    # only loopback may be up, and there is no route anywhere.
    assert probe["interfaces_up"] == ["lo"]
    assert probe["ipv4_routes"] == []
    assert probe["connect_ip"] != "allowed"
    assert probe["resolve"] != "allowed"
    for path in ("write_root", "write_tmp", "write_site"):
        assert probe[path] != "allowed", path
    assert probe["rlimit_cpu"] == [30, 31]
    assert probe["rlimit_fsize"] == [0, 0]
    assert probe["rlimit_nofile"] == [64, 64]
    assert probe["rlimit_core"] == [0, 0]
    assert probe["memory_max"] == str(512 * 1024 * 1024)
    assert probe["pids_max"] == "32"
    assert "PARSER_SANDBOX_TEST_SECRET" not in probe["env"]
    assert set(probe["env"]) <= {
        "LC_CTYPE",  # set by Python itself (PEP 538 locale coercion)
        "PATH",
        "HOME",
        "HOSTNAME",
        "LANG",
        "GPG_KEY",
        "PYTHON_VERSION",
        "PYTHON_SHA256",
        "PYTHONDONTWRITEBYTECODE",
        "PIP_NO_CACHE_DIR",
        "PIP_DISABLE_PIP_VERSION_CHECK",
    }


@real
def test_the_parser_runs_without_pip_as_the_unprivileged_user(settings: SandboxSettings) -> None:
    result = subprocess.run(
        [
            DOCKER,
            "image",
            "inspect",
            "--format",
            "{{.Config.User}} {{json .Config.Entrypoint}}",
            settings.image,
        ],
        capture_output=True,
        check=True,
    )
    user, entrypoint = result.stdout.decode().strip().split(" ", 1)
    assert user == "65534:65534"
    assert json.loads(entrypoint) == ["python", "-I", "-m", "docparser.supervise"]


@real
def test_wall_clock_timeout_kills_and_removes_the_container(
    settings: SandboxSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_entrypoint(monkeypatch, ["sleep", "600"])
    outcome = parse_in_sandbox(b"%PDF-", replace(settings, wall_seconds=3))
    assert outcome == ParseFailed("timeout")
    assert _leftover_containers() == []


@real
def test_cpu_limit_is_a_timeout(settings: SandboxSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    _with_entrypoint(monkeypatch, _python("while True: pass"))
    outcome = parse_in_sandbox(b"%PDF-", replace(settings, cpu_seconds=1, wall_seconds=60))
    assert outcome == ParseFailed("timeout")


@real
def test_memory_limit_is_memory_exceeded(
    settings: SandboxSettings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A kernel OOM kill, through the image's supervisor (the production path): the cgroup's
    own oom_kill count decides, whatever Docker's OOMKilled flag says."""
    # The sandbox's log (exit status, out-of-memory flag outcome) explains a failure.
    caplog.set_level(logging.INFO, logger="api.sandbox")
    _with_entrypoint(monkeypatch, _supervised("x = bytearray(1024 * 1024 * 1024); print(len(x))"))
    outcome = parse_in_sandbox(b"%PDF-", replace(settings, memory="64m"))
    assert outcome == ParseFailed("memory_exceeded"), caplog.text


@real
def test_an_ordinary_sigkill_is_not_memory_exceeded(
    settings: SandboxSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parser killed by SIGKILL for any other reason stays parse_failed (fail closed)."""
    _with_entrypoint(
        monkeypatch, _python("import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
    )
    assert parse_in_sandbox(b"%PDF-", settings) == ParseFailed("parse_failed")


@real
def test_an_ordinary_sigkill_through_the_supervisor_is_not_memory_exceeded(
    settings: SandboxSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_entrypoint(
        monkeypatch, _supervised("import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
    )
    assert parse_in_sandbox(b"%PDF-", settings) == ParseFailed("parse_failed")


@real
@pytest.mark.parametrize(
    ("code", "limits", "expected"),
    [
        ("import time; time.sleep(600)", {"wall_seconds": 3}, "timeout"),
        ("while True: pass", {"cpu_seconds": 1, "wall_seconds": 60}, "timeout"),
        ("import sys; sys.stdout.write('x' * (9 * 1024 * 1024))", {}, "output_too_large"),
        ("print('not json')", {}, "output_invalid"),
        ("import sys; sys.exit(3)", {}, "parse_failed"),
        ("import os, signal; os.kill(os.getpid(), signal.SIGSEGV)", {}, "parse_failed"),
    ],
    ids=["wall-clock", "cpu", "output-cap", "invalid-output", "exit-3", "sigsegv"],
)
def test_the_supervisor_keeps_every_other_outcome(
    settings: SandboxSettings,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    limits: dict[str, Any],
    expected: str,
) -> None:
    _with_entrypoint(monkeypatch, _supervised(code))
    assert parse_in_sandbox(b"%PDF-", replace(settings, **limits)) == ParseFailed(expected)
    assert _leftover_containers() == []


@real
def test_output_over_the_cap_is_refused(
    settings: SandboxSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_entrypoint(monkeypatch, _python("import sys; sys.stdout.write('x' * (9 * 1024 * 1024))"))
    assert parse_in_sandbox(b"%PDF-", settings) == ParseFailed("output_too_large")
    assert _leftover_containers() == []


@real
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("print('not json')", "output_invalid"),
        ("print('{\"ok\": true}')", "output_invalid"),
        ("print('[' * 200000 + ']' * 200000)", "output_invalid"),
        ("import sys; sys.exit(3)", "parse_failed"),
        ("import os, signal; os.kill(os.getpid(), signal.SIGSEGV)", "parse_failed"),
    ],
)
def test_untrusted_or_abnormal_output_fails_closed(
    settings: SandboxSettings, monkeypatch: pytest.MonkeyPatch, code: str, expected: str
) -> None:
    _with_entrypoint(monkeypatch, _python(code))
    assert parse_in_sandbox(b"%PDF-", settings) == ParseFailed(expected)


@real
def test_a_missing_image_is_unavailable_never_pulled(settings: SandboxSettings) -> None:
    missing = replace(
        settings, image=f"production-ai-flagship-parser:missing-{uuid.uuid4().hex[:8]}"
    )
    outcome = parse_in_sandbox(build(Document((LABELLED,))), missing)
    assert outcome == ParseFailed("sandbox_unavailable")
    assert _leftover_containers() == []


@real
def test_document_text_never_reaches_the_logs(
    settings: SandboxSettings, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    parse_in_sandbox(build(Document((LABELLED,))), settings)
    parse_in_sandbox(build(Document((LABELLED,), embedded_file=True)), settings)
    # A parser that writes document text to stderr: drained and discarded.
    _with_entrypoint(
        monkeypatch,
        _python("import sys; sys.stderr.write('Northwind Analytics Ltd'); sys.exit(1)"),
    )
    parse_in_sandbox(b"%PDF-", settings)
    assert "Northwind" not in caplog.text
    assert "Harbour" not in caplog.text


@real
def test_the_parsers_own_limits_apply_inside_the_container(settings: SandboxSettings) -> None:
    """Second layer: the entrypoint's rlimits turn a huge allocation into MemoryError."""
    code = (
        "import json, resource\n"
        "from docparser.__main__ import _set_rlimits\n"
        "from docparser.limits import DEFAULT_LIMITS\n"
        "_set_rlimits(DEFAULT_LIMITS)\n"
        "try:\n"
        "    bytearray(600 * 1024 * 1024)\n"
        "    allocation = 'allowed'\n"
        "except MemoryError:\n"
        "    allocation = 'MemoryError'\n"
        "print(json.dumps({'allocation': allocation,\n"
        "    'as': resource.getrlimit(resource.RLIMIT_AS),\n"
        "    'cpu': resource.getrlimit(resource.RLIMIT_CPU),\n"
        "    'nofile': resource.getrlimit(resource.RLIMIT_NOFILE)}))\n"
    )
    name = f"production-ai-flagship-probe-{uuid.uuid4().hex}"
    try:
        result = subprocess.run(
            docker_run_command(settings, name, entrypoint=_python(code)),
            input=b"",
            capture_output=True,
            timeout=120,
            check=True,
        )
    finally:
        subprocess.run([DOCKER, "rm", "--force", name], capture_output=True, check=False)
    probe = json.loads(result.stdout)
    assert probe["allocation"] == "MemoryError"
    assert probe["as"] == [448 * 1024 * 1024] * 2
    assert probe["cpu"] == [30, 31]
    assert probe["nofile"] == [16, 16]
