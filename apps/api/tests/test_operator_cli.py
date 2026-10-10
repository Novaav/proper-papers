"""Operator CLI end to end on app_test, as app_operator (decisions D-A13, D-A15..D-A18)."""

import io
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

from api import operator
from api.database import DatabaseSettings, create_session_factory
from api.main import create_app
from api.operator import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    UserRef,
    create_operator_engine,
    create_tenant,
    revoke_membership,
)
from tests.test_workspace_endpoints import Api, api, issuer  # noqa: F401  (fixtures)

ISSUER = "https://issuer.test/"


@pytest.fixture
def env(operator_settings: DatabaseSettings) -> dict[str, str]:
    return {"DATABASE_URL_OPERATOR": operator_settings.url.render_as_string(hide_password=False)}


Cli = Callable[..., tuple[int, dict[str, Any]]]


@pytest.fixture
def cli(env: dict[str, str]) -> Cli:
    """Run the CLI in-process; returns (exit code, the JSON object it printed)."""

    def run(*args: str, environ: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        out, err = io.StringIO(), io.StringIO()
        code = operator.main(
            list(args), environ=env if environ is None else environ, stdout=out, stderr=err
        )
        printed = out.getvalue() if code == EXIT_OK else err.getvalue()
        assert (out.getvalue() == "") != (err.getvalue() == ""), "exactly one stream is used"
        return code, json.loads(printed)

    return run


def _user(owner_engine: Engine, subject: str | None = None) -> tuple[UUID, str]:
    subject = subject or f"auth0|op-{uuid4()}"
    with owner_engine.begin() as connection:
        user_id: UUID = connection.execute(
            text("INSERT INTO users (issuer, subject) VALUES (:i, :s) RETURNING id"),
            {"i": ISSUER, "s": subject},
        ).scalar_one()
    return user_id, subject


def _rows(owner_engine: Engine, sql: str, **params: object) -> list[tuple[Any, ...]]:
    with owner_engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(sql), params)]


def _audit(owner_engine: Engine, tenant_id: object) -> list[tuple[Any, ...]]:
    return _rows(
        owner_engine,
        "SELECT action, user_id, old_role, new_role, request_id, reason, db_role "
        "FROM operator_audit WHERE tenant_id = :t ORDER BY occurred_at, id",
        t=tenant_id,
    )


def _memberships(owner_engine: Engine, tenant_id: object) -> dict[UUID, str]:
    return dict(
        _rows(
            owner_engine, "SELECT user_id, role FROM memberships WHERE tenant_id = :t", t=tenant_id
        )
    )


def _create(
    cli: Cli, owner: UUID, request_id: str | None = None, name: str = "Acme"
) -> tuple[int, dict[str, Any]]:
    return cli(
        "tenant",
        "create",
        "--name",
        name,
        "--owner-user-id",
        str(owner),
        "--request-id",
        request_id or f"req-{uuid4()}",
        "--reason",
        "onboarding",
    )


def _tenant_count(owner_engine: Engine, name: str) -> int:
    return len(_rows(owner_engine, "SELECT id FROM tenants WHERE name = :n", n=name))


# --- tenant create ---------------------------------------------------------------------------


def test_create_makes_tenant_owner_and_one_audit_row(cli: Cli, owner_engine: Engine) -> None:
    owner, _ = _user(owner_engine)
    request_id = f"onboard-{uuid4()}"

    code, out = _create(cli, owner, request_id)

    assert (code, out["status"], out["owner_user_id"]) == (EXIT_OK, "created", str(owner))
    tenant_id = UUID(out["tenant_id"])
    assert _rows(owner_engine, "SELECT name FROM tenants WHERE id = :t", t=tenant_id) == [("Acme",)]
    assert _memberships(owner_engine, tenant_id) == {owner: "owner"}
    assert _audit(owner_engine, tenant_id) == [
        ("tenant_created", owner, None, "owner", request_id, "onboarding", "app_operator")
    ]


def test_create_by_issuer_and_subject(cli: Cli, owner_engine: Engine) -> None:
    owner, subject = _user(owner_engine)
    code, out = cli(
        "tenant",
        "create",
        "--name",
        "By subject",
        "--owner-issuer",
        ISSUER,
        "--owner-subject",
        subject,
        "--request-id",
        f"r-{uuid4()}",
        "--reason",
        "x",
    )
    assert (code, out["owner_user_id"]) == (EXIT_OK, str(owner))


def test_repeating_a_request_is_idempotent(cli: Cli, owner_engine: Engine) -> None:
    owner, _ = _user(owner_engine)
    name, request_id = f"Idem {uuid4()}", f"r-{uuid4()}"

    first = _create(cli, owner, request_id, name)
    second = _create(cli, owner, request_id, name)

    assert first[1]["status"] == "created"
    assert second == (EXIT_OK, {**first[1], "status": "unchanged"})
    assert _tenant_count(owner_engine, name) == 1
    assert len(_audit(owner_engine, first[1]["tenant_id"])) == 1


def test_reusing_a_request_id_for_something_else_is_refused(cli: Cli, owner_engine: Engine) -> None:
    owner, _ = _user(owner_engine)
    other, _ = _user(owner_engine)
    name, request_id = f"Conflict {uuid4()}", f"r-{uuid4()}"
    tenant_id = _create(cli, owner, request_id, name)[1]["tenant_id"]

    for args in ((owner, "Another name"), (other, name)):
        code, out = _create(cli, args[0], request_id, args[1])
        assert (code, out["error"]) == (EXIT_REFUSED, "request_id_conflict")

    assert _tenant_count(owner_engine, name) == 1
    assert _tenant_count(owner_engine, "Another name") == 0
    assert _memberships(owner_engine, tenant_id) == {owner: "owner"}
    assert len(_audit(owner_engine, tenant_id)) == 1


def test_concurrent_creates_with_one_request_id_make_one_tenant(
    cli: Cli, owner_engine: Engine, operator_settings: DatabaseSettings, admin_engine: Engine
) -> None:
    owner, _ = _user(owner_engine)
    name, request_id = f"Race {uuid4()}", f"r-{uuid4()}"
    engine = create_operator_engine(operator_settings)
    sessions = create_session_factory(engine)
    results: list[tuple[int, dict[str, Any]]] = []

    try:
        with sessions() as first, first.begin():
            created = create_tenant(
                first, name=name, owner=UserRef(user_id=owner), request_id=request_id, reason="a"
            )
            # The second command blocks on the request id's unique index until the first commits.
            thread = threading.Thread(
                target=lambda: results.append(_create(cli, owner, request_id, name))
            )
            thread.start()
            blocked = _wait_until_blocked(admin_engine, thread)
        thread.join(timeout=30)
    finally:
        engine.dispose()

    assert results == [(EXIT_OK, {**created, "status": "unchanged"})]
    assert _tenant_count(owner_engine, name) == 1
    assert len(_audit(owner_engine, created["tenant_id"])) == 1
    assert blocked, "the second command must have waited for the first"


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (("--name", "   "), "invalid_name"),
        (("--name", "x" * 201), "invalid_name"),
        (("--name", "a\x00b"), "invalid_name"),
        (("--reason", " "), "invalid_reason"),
        (("--reason", "r" * 501), "invalid_reason"),
        (("--request-id", "has space"), "invalid_request_id"),
        (("--request-id", "r" * 201), "invalid_request_id"),
        (("--request-id", "é"), "invalid_request_id"),
    ],
)
def test_invalid_create_input_is_refused_and_nothing_written(
    cli: Cli, owner_engine: Engine, args: tuple[str, str], error: str
) -> None:
    owner, _ = _user(owner_engine)
    values = {"--name": f"Valid {uuid4()}", "--request-id": f"r-{uuid4()}", "--reason": "ok"}
    values.update(dict([args]))
    before = _rows(owner_engine, "SELECT count(*) FROM tenants")

    code, out = cli(
        "tenant", "create", "--owner-user-id", str(owner), *[x for kv in values.items() for x in kv]
    )

    assert (code, out["error"]) == (EXIT_REFUSED, error)
    assert _rows(owner_engine, "SELECT count(*) FROM tenants") == before


def test_unknown_owner_is_refused_and_no_user_is_created(cli: Cli, owner_engine: Engine) -> None:
    users_before = _rows(owner_engine, "SELECT count(*) FROM users")
    subject = f"auth0|never-signed-in-{uuid4()}"

    by_id = _create(cli, uuid4())
    by_subject = cli(
        "tenant",
        "create",
        "--name",
        "x",
        "--owner-issuer",
        ISSUER,
        "--owner-subject",
        subject,
        "--request-id",
        f"r-{uuid4()}",
        "--reason",
        "x",
    )

    assert by_id[0] == by_subject[0] == EXIT_REFUSED
    assert by_id[1]["error"] == by_subject[1]["error"] == "unknown_user"
    assert _rows(owner_engine, "SELECT count(*) FROM users") == users_before


def test_a_failure_after_the_first_insert_leaves_nothing(
    cli: Cli, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The audit insert fails in the database (reason over the column's limit)
    # after the tenant and membership were inserted: all of it rolls back.
    owner, _ = _user(owner_engine)
    name = f"Atomic {uuid4()}"
    monkeypatch.setattr(operator, "_check_text", lambda *_args: None)

    code, out = cli(
        "tenant",
        "create",
        "--name",
        name,
        "--owner-user-id",
        str(owner),
        "--request-id",
        f"r-{uuid4()}",
        "--reason",
        "r" * 501,
    )

    assert (code, out) == (
        EXIT_UNAVAILABLE,
        {"error": "database_error", "message": "IntegrityError"},
    )
    assert _tenant_count(owner_engine, name) == 0
    assert _rows(owner_engine, "SELECT count(*) FROM memberships WHERE user_id = :u", u=owner) == [
        (0,)
    ]


# --- membership grant / revoke --------------------------------------------------------------


@pytest.fixture
def tenant(cli: Cli, owner_engine: Engine) -> tuple[UUID, UUID]:
    """(tenant_id, owner_user_id) of a tenant created through the CLI."""
    owner, _ = _user(owner_engine)
    return UUID(_create(cli, owner)[1]["tenant_id"]), owner


def _grant(cli: Cli, tenant_id: UUID, user: UUID, role: str) -> tuple[int, dict[str, Any]]:
    return cli(
        "membership",
        "grant",
        "--tenant-id",
        str(tenant_id),
        "--user-id",
        str(user),
        "--role",
        role,
        "--reason",
        f"grant {role}",
    )


def _revoke(cli: Cli, tenant_id: UUID, user: UUID) -> tuple[int, dict[str, Any]]:
    return cli(
        "membership",
        "revoke",
        "--tenant-id",
        str(tenant_id),
        "--user-id",
        str(user),
        "--reason",
        "offboarding",
    )


def test_grant_change_and_revoke_are_audited_only_when_something_changes(
    cli: Cli, owner_engine: Engine, tenant: tuple[UUID, UUID]
) -> None:
    tenant_id, owner = tenant
    member, _ = _user(owner_engine)

    statuses = [
        _grant(cli, tenant_id, member, "member")[1]["status"],
        _grant(cli, tenant_id, member, "member")[1]["status"],
        _grant(cli, tenant_id, member, "owner")[1]["status"],
        _revoke(cli, tenant_id, member)[1]["status"],
        _revoke(cli, tenant_id, member)[1]["status"],
    ]

    assert statuses == ["granted", "unchanged", "role_changed", "revoked", "unchanged"]
    assert _memberships(owner_engine, tenant_id) == {owner: "owner"}
    assert [row[:4] for row in _audit(owner_engine, tenant_id)] == [
        ("tenant_created", owner, None, "owner"),
        ("membership_granted", member, None, "member"),
        ("membership_role_changed", member, "member", "owner"),
        ("membership_revoked", member, "owner", None),
    ]


def test_the_reviewer_role_is_granted_changed_and_audited(
    cli: Cli, owner_engine: Engine, tenant: tuple[UUID, UUID]
) -> None:
    tenant_id, owner = tenant
    reviewer, _ = _user(owner_engine)
    statuses = [
        _grant(cli, tenant_id, reviewer, "reviewer")[1]["status"],
        _grant(cli, tenant_id, reviewer, "member")[1]["status"],
        _grant(cli, tenant_id, reviewer, "reviewer")[1]["status"],
    ]
    assert statuses == ["granted", "role_changed", "role_changed"]
    assert _memberships(owner_engine, tenant_id) == {owner: "owner", reviewer: "reviewer"}
    assert [row[:4] for row in _audit(owner_engine, tenant_id)][1:] == [
        ("membership_granted", reviewer, None, "reviewer"),
        ("membership_role_changed", reviewer, "reviewer", "member"),
        ("membership_role_changed", reviewer, "member", "reviewer"),
    ]


def test_grant_by_issuer_and_subject(
    cli: Cli, owner_engine: Engine, tenant: tuple[UUID, UUID]
) -> None:
    member, subject = _user(owner_engine)
    code, out = cli(
        "membership",
        "grant",
        "--tenant-id",
        str(tenant[0]),
        "--issuer",
        ISSUER,
        "--subject",
        subject,
        "--role",
        "member",
        "--reason",
        "x",
    )
    assert (code, out["status"], out["user_id"]) == (EXIT_OK, "granted", str(member))


def test_last_owner_cannot_be_revoked_or_demoted(
    cli: Cli, owner_engine: Engine, tenant: tuple[UUID, UUID]
) -> None:
    tenant_id, owner = tenant
    second, _ = _user(owner_engine)

    assert _revoke(cli, tenant_id, owner)[1]["error"] == "last_owner"
    assert _grant(cli, tenant_id, owner, "member")[1]["error"] == "last_owner"
    # With a second owner, one of them may go; then the other is the last.
    _grant(cli, tenant_id, second, "owner")
    assert _grant(cli, tenant_id, owner, "member")[1]["status"] == "role_changed"
    assert _revoke(cli, tenant_id, second) == (
        EXIT_REFUSED,
        {"error": "last_owner", "message": "the tenant's last owner cannot be removed or demoted"},
    )
    assert _memberships(owner_engine, tenant_id) == {owner: "member", second: "owner"}


def test_concurrent_revokes_of_the_last_two_owners_leave_one(
    cli: Cli,
    owner_engine: Engine,
    operator_settings: DatabaseSettings,
    admin_engine: Engine,
    tenant: tuple[UUID, UUID],
) -> None:
    tenant_id, first_owner = tenant
    second_owner, _ = _user(owner_engine)
    _grant(cli, tenant_id, second_owner, "owner")
    engine = create_operator_engine(operator_settings)
    results: list[tuple[int, dict[str, Any]]] = []

    try:
        with create_session_factory(engine)() as session, session.begin():
            revoked = revoke_membership(
                session, tenant_id=tenant_id, user=UserRef(user_id=first_owner), reason="a"
            )
            thread = threading.Thread(
                target=lambda: results.append(_revoke(cli, tenant_id, second_owner))
            )
            thread.start()
            blocked = _wait_until_blocked(admin_engine, thread)
        thread.join(timeout=30)
    finally:
        engine.dispose()

    assert revoked["status"] == "revoked"
    assert [(code, out.get("error")) for code, out in results] == [(EXIT_REFUSED, "last_owner")]
    assert _memberships(owner_engine, tenant_id) == {second_owner: "owner"}
    assert blocked, "the second revoke must have waited for the first"


@pytest.mark.parametrize("command", ["grant", "revoke", "show"])
def test_unknown_tenant_is_refused(cli: Cli, owner_engine: Engine, command: str) -> None:
    user, _ = _user(owner_engine)
    args = {
        "grant": (
            "membership",
            "grant",
            "--user-id",
            str(user),
            "--role",
            "member",
            "--reason",
            "x",
        ),
        "revoke": ("membership", "revoke", "--user-id", str(user), "--reason", "x"),
        "show": ("tenant", "show"),
    }[command]
    code, out = cli(*args, "--tenant-id", str(uuid4()))
    assert (code, out["error"]) == (EXIT_REFUSED, "unknown_tenant")


def test_unknown_user_cannot_be_granted(
    cli: Cli, owner_engine: Engine, tenant: tuple[UUID, UUID]
) -> None:
    code, out = _grant(cli, tenant[0], uuid4(), "member")
    assert (code, out["error"]) == (EXIT_REFUSED, "unknown_user")
    assert len(_audit(owner_engine, tenant[0])) == 1


def test_show_lists_the_tenant_and_its_memberships(
    cli: Cli, owner_engine: Engine, tenant: tuple[UUID, UUID]
) -> None:
    tenant_id, owner = tenant
    member, _ = _user(owner_engine)
    _grant(cli, tenant_id, member, "member")

    code, out = cli("tenant", "show", "--tenant-id", str(tenant_id))

    assert code == EXIT_OK
    assert out == {
        "status": "ok",
        "tenant_id": str(tenant_id),
        "name": "Acme",
        "memberships": [
            {"user_id": str(owner), "role": "owner"},
            {"user_id": str(member), "role": "member"},
        ],
    }


# --- usage, connection and output ------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("tenant",),
        ("tenant", "delete", "--tenant-id", str(uuid4())),
        ("tenant", "show", "--tenant-id", "not-a-uuid"),
        ("tenant", "show", "--tenant-id", "00000000-0000-0000-0000-000000000000"),
        ("membership", "grant", "--tenant-id", str(uuid4()), "--role", "member", "--reason", "x"),
        (
            "membership",
            "grant",
            "--tenant-id",
            str(uuid4()),
            "--user-id",
            str(uuid4()),
            "--subject",
            "s",
            "--role",
            "member",
            "--reason",
            "x",
        ),
        (
            "membership",
            "grant",
            "--tenant-id",
            str(uuid4()),
            "--issuer",
            ISSUER,
            "--role",
            "member",
            "--reason",
            "x",
        ),
        (
            "membership",
            "grant",
            "--tenant-id",
            str(uuid4()),
            "--user-id",
            str(uuid4()),
            "--role",
            "admin",
            "--reason",
            "x",
        ),
        ("membership", "revoke", "--tenant-id", str(uuid4()), "--user-id", str(uuid4())),
        ("tenant", "create", "--name", "x", "--owner-user-id", str(uuid4()), "--reason", "x"),
        (
            "membership",
            "revoke",
            "--tenant-id",
            str(uuid4()),
            "--issuer",
            ISSUER,
            "--subject",
            "a\x00b",
            "--reason",
            "x",
        ),
        (
            "tenant",
            "create",
            "--name",
            "x",
            "--owner-issuer",
            "i\x00",
            "--owner-subject",
            "s",
            "--request-id",
            "r",
            "--reason",
            "x",
        ),
        (
            "membership",
            "revoke",
            "--tenant-id",
            str(uuid4()),
            "--issuer",
            ISSUER,
            "--subject",
            "s" * 256,
            "--reason",
            "x",
        ),
    ],
)
def test_usage_errors_exit_2_without_touching_the_database(cli: Cli, args: tuple[str, ...]) -> None:
    code, out = cli(*args, environ={})
    assert (code, out["error"]) == (EXIT_USAGE, "usage")


def test_wrong_roles_and_missing_configuration_are_refused(
    cli: Cli,
    runtime_settings: DatabaseSettings,
    owner_settings: DatabaseSettings,
    operator_settings: DatabaseSettings,
) -> None:
    admin = os.environ["TEST_DATABASE_URL_ADMIN"]
    urls = {
        "runtime": runtime_settings.url.render_as_string(hide_password=False),
        "owner": owner_settings.url.render_as_string(hide_password=False),
        "admin": admin,
    }
    show = ("tenant", "show", "--tenant-id", str(uuid4()))
    for label, url in urls.items():
        code, out = cli(*show, environ={"DATABASE_URL_OPERATOR": url})
        assert (code, out["error"]) == (EXIT_UNAVAILABLE, "wrong_role"), label
        assert (runtime_settings.url.password or "") not in json.dumps(out)

    assert cli(*show, environ={})[1]["error"] == "misconfigured"
    # DATABASE_URL (the API's runtime URL) is never used by the CLI.
    code, out = cli(*show, environ={"DATABASE_URL": urls["runtime"]})
    assert (code, out["error"]) == (EXIT_UNAVAILABLE, "misconfigured")
    assert "DATABASE_URL_OPERATOR" in out["message"]


def test_unreachable_database_reports_only_the_error_type(cli: Cli) -> None:
    url = "postgresql://app_operator:secret-password@127.0.0.1:1/app_test"
    code, out = cli(
        "tenant", "show", "--tenant-id", str(uuid4()), environ={"DATABASE_URL_OPERATOR": url}
    )
    assert (code, out) == (
        EXIT_UNAVAILABLE,
        {"error": "database_error", "message": "OperationalError"},
    )


def test_module_entry_point_runs(env: dict[str, str]) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "api.operator", "tenant", "show", "--tenant-id", str(uuid4())],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=60,
        cwd=Path(__file__).parents[1],
    )
    assert result.returncode == EXIT_REFUSED
    assert json.loads(result.stderr)["error"] == "unknown_tenant"
    assert result.stdout == ""
    assert env["DATABASE_URL_OPERATOR"].split("@")[0].split(":")[-1] not in result.stderr


# --- With the A3 API ------------------------------------------------------------------------


def test_onboarded_users_get_access_and_lose_it_when_revoked(api: Api, cli: Cli) -> None:  # noqa: F811
    alice, bob = f"auth0|alice-{uuid4()}", f"auth0|bob-{uuid4()}"
    alice_id, bob_id = api.user(alice), api.user(bob)
    tenant_id = _create(cli, alice_id)[1]["tenant_id"]

    assert api.list(tenant_id, alice).status_code == 200
    assert api.list(tenant_id, bob).status_code == 404
    _grant(cli, UUID(tenant_id), bob_id, "member")
    assert api.create(tenant_id, bob, {"name": "Bob's"}).status_code == 201
    _revoke(cli, UUID(tenant_id), bob_id)
    assert api.list(tenant_id, bob).status_code == 404
    assert api.list(tenant_id, alice).status_code == 200


def test_there_is_no_http_administration() -> None:
    # The complete API surface: tenants and memberships are administered only by the CLI.
    documentation = ("/docs", "/redoc", "/openapi.json")
    review = "/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents/{document_id}/review"
    routes = {
        (method, path)
        for route in create_app({}).routes
        if not (path := getattr(route, "path", "")).startswith(documentation)
        for method in getattr(route, "methods", None) or ()
    }
    assert routes == {
        ("GET", "/health/live"),
        ("GET", "/health/ready"),
        ("GET", "/health/scanner"),
        ("GET", "/v1/me"),
        ("GET", "/v1/tenants/{tenant_id}/workspaces"),
        ("POST", "/v1/tenants/{tenant_id}/workspaces"),
        ("GET", "/v1/tenants/{tenant_id}/workspaces/{workspace_id}"),
        ("GET", "/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents"),
        ("POST", "/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents"),
        ("GET", "/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents/{document_id}"),
        (
            "GET",
            "/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents/{document_id}/content",
        ),
        (
            "POST",
            "/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents/{document_id}/processing",
        ),
        (
            "GET",
            "/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents/{document_id}/processing",
        ),
        ("GET", f"{review}"),
        ("POST", f"{review}/decisions"),
        ("POST", f"{review}/approvals"),
        ("GET", f"{review}/approvals/{{version}}/export"),
    }


def _wait_until_blocked(
    admin_engine: Engine, thread: threading.Thread, timeout: float = 20
) -> bool:
    """True once some app_operator session waits on a lock; False if `thread` ends first."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and thread.is_alive():
        with admin_engine.connect() as connection:
            waiting: int = connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE usename = 'app_operator' AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
        if waiting:
            return True
        time.sleep(0.05)
    return False


# --- Lock timeout ----------------------------------------------------------------------------


def test_every_command_runs_with_the_lock_timeout(operator_settings: DatabaseSettings) -> None:
    engine = create_operator_engine(operator_settings)
    try:
        result = operator.run(
            create_session_factory(engine),
            lambda s: {"lock_timeout": s.execute(text("SHOW lock_timeout")).scalar_one()},
        )
    finally:
        engine.dispose()
    assert result == {"lock_timeout": "5s"}
    assert operator.LOCK_TIMEOUT_MS == 5000


def _while_holding(
    operator_settings: DatabaseSettings,
    hold: Callable[[Any], object],
    command: Callable[[], tuple[int, dict[str, Any]]],
) -> tuple[int, dict[str, Any]]:
    """Run `command` while an uncommitted operator transaction holds locks; then roll it back.

    The command runs in a thread so that a missing timeout fails the test
    instead of hanging it.
    """
    engine = create_operator_engine(operator_settings)
    results: list[tuple[int, dict[str, Any]]] = []
    try:
        with create_session_factory(engine)() as holder:
            holder.begin()
            hold(holder)
            thread = threading.Thread(target=lambda: results.append(command()))
            started = time.monotonic()
            thread.start()
            thread.join(timeout=10)
            elapsed = time.monotonic() - started
            timed_out = thread.is_alive()
            holder.rollback()
        thread.join(timeout=30)
    finally:
        engine.dispose()
    assert not timed_out, "the command waited for the lock without a timeout"
    assert elapsed < 5
    assert len(results) == 1
    return results[0]


BUSY = {
    "error": "busy",
    "message": "another operation is holding a lock this command needs; nothing was changed, "
    "retry later",
}


def test_membership_change_times_out_on_a_held_tenant_lock_and_changes_nothing(
    cli: Cli,
    owner_engine: Engine,
    operator_settings: DatabaseSettings,
    tenant: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operator, "LOCK_TIMEOUT_MS", 200)
    tenant_id, owner = tenant
    held, waiting = _user(owner_engine)[0], _user(owner_engine)[0]

    for command in (
        lambda: _grant(cli, tenant_id, waiting, "member"),
        lambda: _revoke(cli, tenant_id, owner),
        lambda: _grant(cli, tenant_id, owner, "member"),
    ):
        result = _while_holding(
            operator_settings,
            lambda s: operator.grant_membership(
                s, tenant_id=tenant_id, user=UserRef(user_id=held), role="member", reason="held"
            ),
            command,
        )
        assert result == (EXIT_UNAVAILABLE, BUSY)

    assert _memberships(owner_engine, tenant_id) == {owner: "owner"}
    assert [row[0] for row in _audit(owner_engine, tenant_id)] == ["tenant_created"]
    # Nothing is left locked or half-done: the same command now succeeds.
    assert _grant(cli, tenant_id, waiting, "member")[1]["status"] == "granted"


def test_tenant_create_times_out_on_a_concurrent_request_and_leaves_no_trace(
    cli: Cli,
    owner_engine: Engine,
    operator_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The first create is uncommitted, so ours waits on the request id's unique
    # index; it times out, and after the first rolls back neither left anything.
    monkeypatch.setattr(operator, "LOCK_TIMEOUT_MS", 200)
    owner, _ = _user(owner_engine)
    name, request_id = f"Busy {uuid4()}", f"r-{uuid4()}"

    result = _while_holding(
        operator_settings,
        lambda s: create_tenant(
            s, name=name, owner=UserRef(user_id=owner), request_id=request_id, reason="held"
        ),
        lambda: _create(cli, owner, request_id, name),
    )

    assert result == (EXIT_UNAVAILABLE, BUSY)
    assert _tenant_count(owner_engine, name) == 0
    assert _rows(
        owner_engine, "SELECT count(*) FROM operator_audit WHERE request_id = :r", r=request_id
    ) == [(0,)]
    assert _rows(owner_engine, "SELECT count(*) FROM memberships WHERE user_id = :u", u=owner) == [
        (0,)
    ]
    # The request id was not consumed.
    assert _create(cli, owner, request_id, name)[1]["status"] == "created"
    assert _tenant_count(owner_engine, name) == 1


def test_busy_output_contains_no_connection_details(
    cli: Cli,
    env: dict[str, str],
    owner_engine: Engine,
    operator_settings: DatabaseSettings,
    tenant: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operator, "LOCK_TIMEOUT_MS", 200)
    tenant_id, _owner = tenant
    member, _ = _user(owner_engine)
    out, err = io.StringIO(), io.StringIO()

    def command() -> tuple[int, dict[str, Any]]:
        args = [
            "membership",
            "grant",
            "--tenant-id",
            str(tenant_id),
            "--user-id",
            str(member),
            "--role",
            "member",
            "--reason",
            "x",
        ]
        code = operator.main(args, environ=env, stdout=out, stderr=err)
        return code, json.loads(err.getvalue())

    _while_holding(
        operator_settings,
        lambda s: operator.revoke_membership(
            s, tenant_id=tenant_id, user=UserRef(user_id=member), reason="h"
        ),
        command,
    )
    password = operator_settings.url.password or ""
    assert out.getvalue() == ""
    assert password and password not in err.getvalue()
    assert "postgresql" not in err.getvalue() and "lock_timeout" not in err.getvalue()
