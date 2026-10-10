"""The clamd client against an in-process fake: framing, verdicts, fail-closed parsing."""

import logging
import os
import shutil
import socket
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from api.scanning import (
    CHUNK_BYTES,
    ClamdScanner,
    ScannerConfigError,
    ScannerUnavailable,
    Verdict,
    parse_address,
)
from tests.fake_clamd import FakeClamd, ctime, fresh_version

PDF = b"%PDF-1.7\n% synthetic test document\n%%EOF\n"


def _unavailable(scanner: ClamdScanner, content: bytes = PDF) -> str:
    with pytest.raises(ScannerUnavailable) as error:
        scanner.scan(content)
    return error.value.reason


def test_a_clean_reply_is_clean_and_the_bytes_arrive_exactly(fake_clamd: FakeClamd) -> None:
    content = PDF + bytes(range(256)) * ((3 * CHUNK_BYTES) // 256 + 7)  # several chunks
    result = ClamdScanner(fake_clamd.address).scan(content)
    assert result.verdict is Verdict.CLEAN
    assert result.signature_name is None
    assert (result.signatures.engine, result.signatures.engine_version) == ("ClamAV", "1.4.6")
    assert result.signatures.signature_version == "27777"
    assert fake_clamd.streams == [content]
    assert fake_clamd.commands == [b"zVERSION", b"zINSTREAM"]


@pytest.mark.parametrize(
    ("reply", "verdict"),
    [
        (b"stream: Eicar-Test-Signature FOUND", Verdict.INFECTED),
        (b"stream: ProductionAiFlagship.Test.Marker.UNOFFICIAL FOUND", Verdict.INFECTED),
        (b"stream: Heuristics.Limits.Exceeded.MaxFileSize FOUND", Verdict.INCOMPLETE),
        (b"stream: Heuristics.Limits.Exceeded FOUND", Verdict.INCOMPLETE),
        (b"stream: Heuristics.Encrypted.PDF FOUND", Verdict.ENCRYPTED),
    ],
)
def test_detections_are_verdicts_with_their_name(
    fake_clamd: FakeClamd, reply: bytes, verdict: Verdict
) -> None:
    fake_clamd.scan_reply = reply
    result = ClamdScanner(fake_clamd.address).scan(PDF)
    assert result.verdict is verdict
    assert result.signature_name == reply.removeprefix(b"stream: ").removesuffix(b" FOUND").decode()


@pytest.mark.parametrize(
    "reply",
    [
        b"stream: Can't allocate memory ERROR",
        b"INSTREAM size limit exceeded. ERROR",
        b"stream: OK extra",
        b"stream: ok",
        b"stream:  OK",
        b" stream: OK",
        b"stream: OK\n",
        b"1: stream: OK",
        b"stream: Name With Spaces FOUND",
        b"stream: " + b"X" * 201 + b" FOUND",
        b"stream: \xff FOUND",
        b"COMMAND UNAVAILABLE",
        b"",
    ],
)
def test_any_other_reply_is_unavailable(fake_clamd: FakeClamd, reply: bytes) -> None:
    fake_clamd.scan_reply = reply
    assert _unavailable(ClamdScanner(fake_clamd.address)) == "reply"


@pytest.mark.parametrize(
    "raw",
    [
        None,  # connection closed without a reply
        b"stream: OK",  # no NUL terminator before the close
        b"x" * 2000,  # longer than any reply
    ],
)
def test_truncated_or_oversized_replies_are_unavailable(
    fake_clamd: FakeClamd, raw: bytes | None
) -> None:
    fake_clamd.scan_reply = None
    fake_clamd.scan_raw = raw
    assert _unavailable(ClamdScanner(fake_clamd.address)) == "reply"


def test_a_slow_scanner_times_out(fake_clamd: FakeClamd) -> None:
    fake_clamd.delay = 1.0
    assert _unavailable(ClamdScanner(fake_clamd.address, timeout=0.3)) == "timeout"


def test_an_unreachable_scanner_is_unavailable() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert _unavailable(ClamdScanner(f"tcp://127.0.0.1:{port}")) == "transport"


def test_concurrent_scans_beyond_the_limit_are_refused(fake_clamd: FakeClamd) -> None:
    fake_clamd.delay = 0.5
    scanner = ClamdScanner(fake_clamd.address, max_concurrent=1)
    results: list[Verdict] = []
    first = threading.Thread(target=lambda: results.append(scanner.scan(PDF).verdict))
    first.start()
    while not fake_clamd.commands:  # the first scan holds the only slot
        time.sleep(0.005)
    assert _unavailable(scanner) == "busy"
    first.join()
    assert results == [Verdict.CLEAN]


@pytest.mark.parametrize(
    ("version", "reason"),
    [
        (None, "reply"),
        (b"COMMAND UNAVAILABLE", "signatures_unknown"),
        (b"ClamAV 1.4.6", "signatures_unknown"),
        (b"ClamAV 1.4.6/27777/yesterday afternoon", "signatures_unknown"),
        (b"ClamAV 1.4.6/27777/Mon Foo 99 99:99:99 2026", "signatures_unknown"),
    ],
)
def test_unknown_signatures_are_unavailable(
    fake_clamd: FakeClamd, version: bytes | None, reason: str
) -> None:
    fake_clamd.version_reply = version
    assert _unavailable(ClamdScanner(fake_clamd.address)) == reason
    assert fake_clamd.streams == []  # nothing is scanned without known signatures


def test_signatures_older_than_the_limit_are_unavailable(fake_clamd: FakeClamd) -> None:
    fake_clamd.version_reply = fresh_version(age=timedelta(hours=73))
    assert _unavailable(ClamdScanner(fake_clamd.address)) == "signatures_stale"
    assert fake_clamd.streams == []


def test_ageing_signatures_warn_and_fresh_ones_do_not(
    fake_clamd: FakeClamd, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="api.scanning"):
        ClamdScanner(fake_clamd.address).scan(PDF)
        assert "hours old" not in caplog.text
        fake_clamd.version_reply = fresh_version(age=timedelta(hours=30))
        assert ClamdScanner(fake_clamd.address).scan(PDF).verdict is Verdict.CLEAN
    assert "scanner signatures are 30 hours old" in caplog.text


def test_the_age_is_checked_on_every_scan_even_when_cached(fake_clamd: FakeClamd) -> None:
    built = datetime(2026, 9, 30, 12, tzinfo=UTC)
    fake_clamd.version_reply = b"ClamAV 1.4.6/27777/" + ctime(built)
    clock = [built + timedelta(hours=1)]
    scanner = ClamdScanner(fake_clamd.address, now=lambda: clock[0])
    assert scanner.scan(PDF).verdict is Verdict.CLEAN
    clock[0] = built + timedelta(hours=80)
    assert _unavailable(scanner) == "signatures_stale"
    assert fake_clamd.commands.count(b"zVERSION") == 1  # the cached value was re-checked


def test_the_version_is_cached_between_scans(fake_clamd: FakeClamd) -> None:
    scanner = ClamdScanner(fake_clamd.address)
    scanner.scan(PDF)
    scanner.scan(PDF)
    assert fake_clamd.commands == [b"zVERSION", b"zINSTREAM", b"zINSTREAM"]
    expired = ClamdScanner(fake_clamd.address, version_cache_seconds=0)
    fake_clamd.reset()
    expired.scan(PDF)
    expired.scan(PDF)
    assert fake_clamd.commands.count(b"zVERSION") == 2


def test_addresses() -> None:
    assert parse_address("tcp://127.0.0.1:3310") == (socket.AF_UNSPEC, ("127.0.0.1", 3310))
    assert parse_address("tcp://[::1]:3310") == (socket.AF_UNSPEC, ("::1", 3310))
    assert parse_address("tcp://clamd.internal:3310") == (
        socket.AF_UNSPEC,
        ("clamd.internal", 3310),
    )
    assert parse_address("unix:///run/clamav/clamd.sock") == (
        socket.AF_UNIX,
        "/run/clamav/clamd.sock",
    )
    for bad in [
        "",
        "127.0.0.1:3310",
        "http://clamd:3310",
        "tcp://clamd",
        "tcp://:3310",
        "tcp://clamd:0",
        "tcp://clamd:65536",
        "tcp://clamd:x",
        "unix://relative.sock",
    ]:
        with pytest.raises(ScannerConfigError):
            parse_address(bad)


@pytest.mark.parametrize("name", ["clamd.conf", "clamd-tight.conf"])
def test_the_engine_configurations_report_limits_and_encryption(name: str) -> None:
    # Both settings are off by default in clamd; without them exceeded limits
    # and encrypted documents or archives (also embedded ones) pass as clean.
    lines = {
        line.split(None, 1)[0]: line.split(None, 1)[1].strip()
        for line in (Path(__file__).parent / "clamav" / name).read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert lines["AlertExceedsMax"] == "yes"
    assert lines["AlertEncrypted"] == "yes"
    # The narrower options would silently drop one of the two families.
    assert "AlertEncryptedDoc" not in lines and "AlertEncryptedArchive" not in lines


def _ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
        return True
    except OSError:
        return False


def test_a_host_name_is_resolved(fake_clamd: FakeClamd) -> None:
    port = fake_clamd.address.rpartition(":")[2]
    assert ClamdScanner(f"tcp://localhost:{port}").scan(PDF).verdict is Verdict.CLEAN


@pytest.mark.skipif(not _ipv6_loopback(), reason="no IPv6 loopback in this environment")
def test_an_ipv6_address_connects() -> None:
    fake = FakeClamd(host="::1")
    try:
        assert fake.address.startswith("tcp://[::1]:")
        assert ClamdScanner(fake.address).scan(PDF).verdict is Verdict.CLEAN
        assert fake.streams == [PDF]
    finally:
        fake.close()


def test_a_unix_socket_scanner_is_used_end_to_end() -> None:
    # S5: the AF_UNIX path (short path: socket paths are limited to ~104 bytes).
    directory = tempfile.mkdtemp(prefix="clamd-", dir="/tmp")
    fake = FakeClamd(unix_path=os.path.join(directory, "clamd.sock"))
    try:
        assert fake.address.startswith("unix:///tmp/clamd-")
        scanner = ClamdScanner(fake.address)
        assert scanner.scan(PDF).verdict is Verdict.CLEAN
        assert fake.streams == [PDF]
        scanner.check()
        fake.scan_reply = b"stream: Eicar-Test-Signature FOUND"
        assert scanner.scan(PDF).verdict is Verdict.INFECTED
    finally:
        fake.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_check_passes_for_an_answering_scanner_with_fresh_signatures(
    fake_clamd: FakeClamd,
) -> None:
    ClamdScanner(fake_clamd.address).check()
    assert fake_clamd.commands == [b"zPING", b"zVERSION"]
    assert fake_clamd.streams == []  # a health check never scans


@pytest.mark.parametrize(
    ("ping", "version", "reason"),
    [
        (None, fresh_version(), "reply"),
        (b"PANG", fresh_version(), "reply"),
        (b"PONG", None, "reply"),
        (b"PONG", b"COMMAND UNAVAILABLE", "signatures_unknown"),
        (b"PONG", fresh_version(age=timedelta(hours=73)), "signatures_stale"),
    ],
)
def test_check_fails_closed_like_a_scan(
    fake_clamd: FakeClamd, ping: bytes | None, version: bytes | None, reason: str
) -> None:
    fake_clamd.ping_reply = ping
    fake_clamd.version_reply = version
    with pytest.raises(ScannerUnavailable) as error:
        ClamdScanner(fake_clamd.address).check()
    assert error.value.reason == reason


def test_check_of_an_unreachable_scanner_fails_closed() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(ScannerUnavailable) as error:
        ClamdScanner(f"tcp://127.0.0.1:{port}").check()
    assert error.value.reason == "transport"
