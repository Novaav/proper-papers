"""Readiness, liveness and the engine lifecycle of the FastAPI application."""

import ast
import json
import logging
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event

import api.main
from api.database import DatabaseSettings, create_runtime_engine
from api.main import create_app

UNAVAILABLE = {"status": "unavailable"}
SECRET = "readiness-s3cr3t"


def _url(settings: DatabaseSettings, password: str | None = None) -> str:
    url = settings.url if password is None else settings.url.set(password=password)
    return url.render_as_string(hide_password=False)


@pytest.fixture
def silent_listener() -> Iterator[int]:
    """A local TCP port that accepts connections but never answers."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        yield listener.getsockname()[1]


@pytest.fixture
def sql_statements() -> Iterator[list[str]]:
    sent: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        sent.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    yield sent
    event.remove(Engine, "before_cursor_execute", record)


def test_ready_when_the_database_answers_as_the_runtime_role(
    runtime_settings: DatabaseSettings,
) -> None:
    with TestClient(create_app({"DATABASE_URL": _url(runtime_settings)})) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"DATABASE_URL": ""},
        {"DATABASE_URL": f"not a url {SECRET}"},
        {"DATABASE_URL": f"sqlite:///{SECRET}.db"},
        {"DATABASE_URL": f"postgresql+psycopg2://app:{SECRET}@127.0.0.1/app"},
    ],
)
def test_not_ready_without_valid_configuration(
    environ: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO), TestClient(create_app(environ)) as client:
        ready = client.get("/health/ready")
        live = client.get("/health/live")

    assert (ready.status_code, ready.json()) == (503, UNAVAILABLE)
    assert live.status_code == 200
    assert "database is not configured" in caplog.text
    assert SECRET not in caplog.text


def test_not_ready_when_the_database_is_unreachable(caplog: pytest.LogCaptureFixture) -> None:
    # Port 1 on localhost refuses connections, as a stopped server does.
    environ = {"DATABASE_URL": f"postgresql://app_runtime:{SECRET}@127.0.0.1:1/app_test"}

    with caplog.at_level(logging.INFO), TestClient(create_app(environ)) as client:
        response = client.get("/health/ready")

    assert (response.status_code, response.json()) == (503, UNAVAILABLE)
    assert SECRET not in response.text and SECRET not in caplog.text
    assert "127.0.0.1" not in response.text


def test_not_ready_with_wrong_credentials(
    runtime_settings: DatabaseSettings, caplog: pytest.LogCaptureFixture
) -> None:
    environ = {"DATABASE_URL": _url(runtime_settings, password=SECRET)}

    with caplog.at_level(logging.INFO), TestClient(create_app(environ)) as client:
        response = client.get("/health/ready")

    assert (response.status_code, response.json()) == (503, UNAVAILABLE)
    assert SECRET not in caplog.text
    assert "password" not in response.text.lower()


def test_not_ready_when_configured_with_the_schema_owner(owner_settings: DatabaseSettings) -> None:
    # The runtime role guard refuses owners, superusers and BYPASSRLS roles.
    with TestClient(create_app({"DATABASE_URL": _url(owner_settings)})) as client:
        response = client.get("/health/ready")

    assert (response.status_code, response.json()) == (503, UNAVAILABLE)


def test_readiness_gives_up_within_the_connect_timeout(silent_listener: int) -> None:
    environ = {"DATABASE_URL": f"postgresql://app_runtime:pw@127.0.0.1:{silent_listener}/app"}

    with TestClient(create_app(environ)) as client:
        started = time.monotonic()
        response = client.get("/health/ready")
        elapsed = time.monotonic() - started

    assert (response.status_code, response.json()) == (503, UNAVAILABLE)
    assert elapsed < api.main.CONNECT_TIMEOUT_SECONDS + 3


def test_liveness_runs_no_sql_and_survives_database_failure(
    runtime_settings: DatabaseSettings, sql_statements: list[str]
) -> None:
    with TestClient(create_app({"DATABASE_URL": _url(runtime_settings)})) as client:
        assert client.get("/health/live").status_code == 200
        assert sql_statements == []
        assert client.get("/health/ready").status_code == 200
        assert sql_statements != []  # the recorder does see SQL when it runs

    environ = {"DATABASE_URL": "postgresql://app_runtime:pw@127.0.0.1:1/app_test"}
    with TestClient(create_app(environ)) as client:
        assert client.get("/health/ready").status_code == 503
        live = client.get("/health/live")
    assert (live.status_code, live.json()) == (200, {"status": "ok"})


def test_one_engine_per_lifespan_reused_and_disposed(
    runtime_settings: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[Engine] = []
    disposed: list[Engine] = []
    real_create = create_runtime_engine

    def recording_create(*args: Any, **kwargs: Any) -> Engine:
        engine = real_create(*args, **kwargs)
        real_dispose = engine.dispose

        def recording_dispose(close: bool = True) -> None:
            disposed.append(engine)
            real_dispose(close)

        monkeypatch.setattr(engine, "dispose", recording_dispose)
        created.append(engine)
        return engine

    monkeypatch.setattr(api.main, "create_runtime_engine", recording_create)
    app = create_app({"DATABASE_URL": _url(runtime_settings)})

    assert created == []  # building the app creates nothing
    with TestClient(app) as client:
        for _ in range(5):
            assert client.get("/health/ready").status_code == 200
        assert len(created) == 1
        assert app.state.engine is created[0]
        assert created[0].pool.checkedout() == 0
        assert disposed == []

    assert disposed == created
    assert app.state.engine is None


def test_application_code_never_imports_alembic() -> None:
    source_root = Path(__file__).parents[1] / "src" / "api"
    imports = [
        f"{path.name}:{node.lineno}"
        for path in source_root.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if (isinstance(node, ast.Import) and any(a.name.startswith("alembic") for a in node.names))
        or (isinstance(node, ast.ImportFrom) and (node.module or "").startswith("alembic"))
    ]

    assert imports == []


# Runs in a fresh interpreter so that importing api.main is observed from scratch.
_IMPORT_PROBE = """
import json, os, select, socket, sys

accessed = []

class RecordingEnviron(dict):
    def __getitem__(self, key):
        accessed.append(key)
        return super().__getitem__(key)
    def get(self, key, default=None):
        accessed.append(key)
        return super().get(key, default)
    def __contains__(self, key):
        accessed.append(key)
        return super().__contains__(key)

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(8)
port = listener.getsockname()[1]
os.environ = RecordingEnviron(os.environ)
os.environ["DATABASE_URL"] = f"postgresql://app_runtime:pw@127.0.0.1:{port}/app"
accessed.clear()

def connection_pending():
    return bool(select.select([listener], [], [], 0.3)[0])

import api.main
result = {
    "env_read_at_import": [k for k in accessed if "DATABASE" in k],
    "connected_at_import": connection_pending(),
}

from fastapi.testclient import TestClient
with TestClient(api.main.app) as client:
    result["connected_at_startup"] = connection_pending()
    result["live"] = client.get("/health/live").status_code
    result["connected_by_liveness"] = connection_pending()
    result["env_read_at_startup"] = sorted({k for k in accessed if "DATABASE" in k})
    result["ready"] = client.get("/health/ready").status_code
    result["connected_by_readiness"] = connection_pending()
result["alembic_imported"] = any(name.startswith("alembic") for name in sys.modules)
print(json.dumps(result))
"""


def test_import_and_startup_do_not_connect_and_only_startup_reads_configuration() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert result == {
        "env_read_at_import": [],
        "connected_at_import": False,
        "connected_at_startup": False,
        "live": 200,
        "connected_by_liveness": False,
        "env_read_at_startup": ["DATABASE_URL"],
        "ready": 503,
        "connected_by_readiness": True,  # positive control: the probe detects connections
        "alembic_imported": False,
    }
