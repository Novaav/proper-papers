"""Malware scanning of uploads with ClamAV (clamd), over its INSTREAM protocol.

Only an exact `stream: OK` reply is clean. A detection is a verdict (infected,
incomplete, encrypted); anything else is `ScannerUnavailable`: an ERROR or
unexpected reply, a transport failure, a timeout, a busy scanner, or signature
databases that are unknown or older than the allowed age. The caller stores
nothing unless the verdict is clean (fail closed).

A clean verdict means that no known signature matched in the content ClamAV
inspected, not that every byte was inspected: clamd must run with
AlertExceedsMax and AlertEncrypted enabled so that exceeded limits and
encrypted documents or archives are reported instead of passing as clean (see
docs/deployment-security.md). The client never logs file content or names.
"""

import logging
import re
import socket
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

logger = logging.getLogger(__name__)

CHUNK_BYTES = 64 * 1024
MAX_REPLY_BYTES = 1024
HEALTH_TIMEOUT_SECONDS = 5.0

_OK = re.compile(rb"stream: OK")
# Signature names are printable ASCII without spaces; anything else is not a
# reply this client understands, so it counts as unavailable.
_FOUND = re.compile(rb"stream: ([\x21-\x7e]{1,200}) FOUND")
_VERSION = re.compile(rb"ClamAV ([0-9][0-9A-Za-z.+-]{0,99})/([0-9]{1,20})/([ -~]{10,40})")
_LIMITS_EXCEEDED = b"Heuristics.Limits.Exceeded"
_ENCRYPTED = b"Heuristics.Encrypted"


class Verdict(StrEnum):
    CLEAN = "clean"
    INFECTED = "infected"
    INCOMPLETE = "incomplete"
    ENCRYPTED = "encrypted"


@dataclass(frozen=True, slots=True)
class Signatures:
    """The engine and signature databases a verdict was reached with."""

    engine: str
    engine_version: str
    signature_version: str
    signature_date: datetime


@dataclass(frozen=True, slots=True)
class ScanResult:
    verdict: Verdict
    signatures: Signatures
    # The detection name for a non-clean verdict (printable ASCII, <= 200 chars).
    signature_name: str | None


class ScannerUnavailable(Exception):
    """No acceptable verdict could be obtained; `reason` is a fixed category."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ScannerConfigError(ValueError):
    """The scanner address is missing or malformed."""


def parse_address(value: str) -> tuple[int, str | tuple[str, int]]:
    """`tcp://host:port` or `unix:///absolute/path` -> (address family, address).

    TCP addresses are returned with AF_UNSPEC: the host is resolved when
    connecting (IPv4, IPv6 or a name); IPv6 literals are written in brackets.
    """
    if value.startswith("unix://"):
        path = value.removeprefix("unix://")
        if not path.startswith("/"):
            raise ScannerConfigError("unix socket path must be absolute")
        return socket.AF_UNIX, path
    if value.startswith("tcp://"):
        host, sep, port = value.removeprefix("tcp://").rpartition(":")
        if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise ScannerConfigError("expected tcp://host:port")
        return socket.AF_UNSPEC, (host.strip("[]"), int(port))
    raise ScannerConfigError("expected tcp://host:port or unix:///path")


class ClamdScanner:
    """A thread-safe clamd client: one connection per request, bounded concurrency."""

    def __init__(
        self,
        address: str,
        *,
        timeout: float = 30.0,
        connect_timeout: float = 2.0,
        max_concurrent: int = 4,
        max_signature_age: timedelta = timedelta(hours=72),
        warn_signature_age: timedelta = timedelta(hours=24),
        version_cache_seconds: float = 60.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._family, self._address = parse_address(address)
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._max_age = max_signature_age
        self._warn_age = warn_signature_age
        self._cache_seconds = version_cache_seconds
        self._now = now
        self._lock = threading.Lock()
        self._cached: tuple[float, Signatures] | None = None

    def scan(self, content: bytes) -> ScanResult:
        """Scan `content`; raises ScannerUnavailable unless a verdict is obtained."""
        if not self._slots.acquire(blocking=False):
            raise ScannerUnavailable("busy")
        try:
            deadline = time.monotonic() + self._timeout
            signatures = self._signatures(deadline)
            reply = self._request(b"zINSTREAM\0", content, deadline)
            verdict, name = _verdict(reply)
            return ScanResult(verdict=verdict, signatures=signatures, signature_name=name)
        finally:
            self._slots.release()

    def check(self) -> None:
        """Raise ScannerUnavailable unless clamd answers and its signatures are
        acceptable; the same rules as a scan (PING, then VERSION and freshness),
        without scanning anything. Bounded by HEALTH_TIMEOUT_SECONDS."""
        deadline = time.monotonic() + min(self._timeout, HEALTH_TIMEOUT_SECONDS)
        if self._request(b"zPING\0", None, deadline) != b"PONG":
            raise ScannerUnavailable("reply")
        self._signatures(deadline)

    def _signatures(self, deadline: float) -> Signatures:
        with self._lock:
            cached = self._cached
        if cached is not None and time.monotonic() - cached[0] < self._cache_seconds:
            signatures = cached[1]
        else:
            signatures = _parse_version(self._request(b"zVERSION\0", None, deadline))
            with self._lock:
                self._cached = (time.monotonic(), signatures)
        # Checked on every scan, also for a cached value.
        age = self._now() - signatures.signature_date
        if age > self._max_age:
            raise ScannerUnavailable("signatures_stale")
        if age > self._warn_age:
            logger.warning("scanner signatures are %d hours old", age // timedelta(hours=1))
        return signatures

    def _request(self, command: bytes, content: bytes | None, deadline: float) -> bytes:
        try:
            with self._connect(deadline) as sock:
                sock.settimeout(_remaining(deadline))
                sock.sendall(command)
                if content is not None:
                    for offset in range(0, len(content), CHUNK_BYTES):
                        chunk = content[offset : offset + CHUNK_BYTES]
                        sock.settimeout(_remaining(deadline))
                        sock.sendall(struct.pack(">I", len(chunk)) + chunk)
                    sock.settimeout(_remaining(deadline))
                    sock.sendall(b"\0\0\0\0")
                return _read_reply(sock, deadline)
        except TimeoutError:
            raise ScannerUnavailable("timeout") from None
        except OSError:
            raise ScannerUnavailable("transport") from None

    def _connect(self, deadline: float) -> socket.socket:
        """A connected socket, within the connect timeout and the scan deadline."""
        timeout = min(self._connect_timeout, _remaining(deadline))
        if self._family == socket.AF_UNIX:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.settimeout(timeout)
                sock.connect(self._address)
            except BaseException:
                sock.close()
                raise
            return sock
        # Resolves the host and tries each address (IPv4 and IPv6) in turn.
        if not isinstance(self._address, tuple):
            raise ScannerUnavailable("transport")
        return socket.create_connection(self._address, timeout=timeout)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ScannerUnavailable("timeout")
    return remaining


def _read_reply(sock: socket.socket, deadline: float) -> bytes:
    """One NUL-terminated reply record (z-prefixed commands), at most MAX_REPLY_BYTES."""
    data = bytearray()
    while b"\0" not in data:
        sock.settimeout(_remaining(deadline))
        chunk = sock.recv(MAX_REPLY_BYTES)
        if not chunk:
            raise ScannerUnavailable("reply")
        data += chunk
        if len(data) > MAX_REPLY_BYTES:
            raise ScannerUnavailable("reply")
    return bytes(data[: data.index(b"\0")])


def _verdict(reply: bytes) -> tuple[Verdict, str | None]:
    if _OK.fullmatch(reply):
        return Verdict.CLEAN, None
    found = _FOUND.fullmatch(reply)
    if found is None:
        raise ScannerUnavailable("reply")
    name = found.group(1)
    if name.startswith(_LIMITS_EXCEEDED):
        verdict = Verdict.INCOMPLETE
    elif name.startswith(_ENCRYPTED):
        verdict = Verdict.ENCRYPTED
    else:
        verdict = Verdict.INFECTED
    return verdict, name.decode("ascii")


def _parse_version(reply: bytes) -> Signatures:
    """`ClamAV <engine>/<database version>/<database date>`; anything else is unavailable."""
    match = _VERSION.fullmatch(reply)
    if match is None:
        raise ScannerUnavailable("signatures_unknown")
    engine_version, database, date = (part.decode("ascii") for part in match.groups())
    try:
        # clamd prints the database build time in C ctime() format. It is
        # read as UTC; a clamd running in another time zone shifts it by at
        # most a few hours, well within the allowed signature age.
        built = datetime.strptime(date, "%a %b %d %H:%M:%S %Y").replace(tzinfo=UTC)
    except ValueError:
        raise ScannerUnavailable("signatures_unknown") from None
    return Signatures(
        engine="ClamAV",
        engine_version=engine_version,
        signature_version=database,
        signature_date=built,
    )
