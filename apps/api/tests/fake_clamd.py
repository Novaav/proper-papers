"""An in-process stand-in for clamd, speaking its z-command protocol (tests only).

It answers VERSION and INSTREAM like clamd, reassembles each INSTREAM body so
tests can check exactly what was sent, and can be told to misbehave (errors,
garbage, missing terminators, closed connections, delays).
"""

import socket
import socketserver
import struct
import threading
import time
from datetime import UTC, datetime, timedelta


def ctime(moment: datetime) -> bytes:
    """A database date as clamd prints it (C ctime() format)."""
    return moment.strftime("%a %b %d %H:%M:%S %Y").encode()


def fresh_version(age: timedelta = timedelta(hours=1)) -> bytes:
    return b"ClamAV 1.4.6/27777/" + ctime(datetime.now(UTC) - age)


class FakeClamd:
    def __init__(self, host: str = "127.0.0.1", unix_path: str | None = None) -> None:
        fake = self
        family = socket.AF_INET6 if ":" in host else socket.AF_INET

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                fake._handle(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True
            address_family = family

        class UnixServer(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self._unix_path = unix_path
        self._server: socketserver.BaseServer = (
            UnixServer(unix_path, Handler) if unix_path else Server((host, 0), Handler)
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._lock = threading.Lock()
        self.reset()

    @property
    def address(self) -> str:
        if self._unix_path:
            return f"unix://{self._unix_path}"
        address = self._server.server_address
        assert isinstance(address, tuple)
        host, port = str(address[0]), address[1]
        return f"tcp://[{host}]:{port}" if ":" in host else f"tcp://{host}:{port}"

    def reset(self) -> None:
        with self._lock:
            # Replies without the NUL terminator; None closes without replying.
            self.ping_reply: bytes | None = b"PONG"
            self.version_reply: bytes | None = fresh_version()
            self.scan_reply: bytes | None = b"stream: OK"
            # Raw bytes sent instead of scan_reply + NUL (for framing tests).
            self.scan_raw: bytes | None = None
            # When set, a stream containing these bytes gets a FOUND reply (like
            # the real engine with the test-only marker signature).
            self.infected_marker: bytes | None = None
            self.delay = 0.0
            self.commands: list[bytes] = []
            self.streams: list[bytes] = []

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _handle(self, sock: socket.socket) -> None:
        command = _read_until_nul(sock)
        if command is None:
            return
        with self._lock:
            self.commands.append(command)
        if command == b"zPING":
            if self.ping_reply is not None:
                sock.sendall(self.ping_reply + b"\0")
            return
        if command == b"zVERSION":
            if self.version_reply is not None:
                sock.sendall(self.version_reply + b"\0")
            return
        if command != b"zINSTREAM":
            sock.sendall(b"UNKNOWN COMMAND\0")
            return
        body = bytearray()
        while True:
            header = _read_exact(sock, 4)
            if header is None:
                return
            (length,) = struct.unpack(">I", header)
            if length == 0:
                break
            chunk = _read_exact(sock, length)
            if chunk is None:
                return
            body += chunk
        with self._lock:
            self.streams.append(bytes(body))
        if self.delay:
            time.sleep(self.delay)
        if self.infected_marker is not None and self.infected_marker in body:
            sock.sendall(b"stream: ProductionAiFlagship.Test.Marker.UNOFFICIAL FOUND\0")
        elif self.scan_raw is not None:
            sock.sendall(self.scan_raw)
        elif self.scan_reply is not None:
            sock.sendall(self.scan_reply + b"\0")


def _read_until_nul(sock: socket.socket) -> bytes | None:
    data = bytearray()
    while True:
        byte = sock.recv(1)
        if not byte:
            return None
        if byte == b"\0":
            return bytes(data)
        data += byte


def _read_exact(sock: socket.socket, count: int) -> bytes | None:
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            return None
        data += chunk
    return bytes(data)


_shared: FakeClamd | None = None


def shared() -> FakeClamd:
    """One fake per test session (the endpoint tests' CLAMD_ADDRESS)."""
    global _shared
    if _shared is None:
        _shared = FakeClamd()
    return _shared
