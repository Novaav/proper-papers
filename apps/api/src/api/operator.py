"""Operator CLI: create tenants and administer memberships. Never exposed over HTTP.

    python -m api.operator tenant create --name N --owner-user-id U --request-id R --reason T
    python -m api.operator membership grant --tenant-id T --user-id U \
        --role owner|reviewer|member --reason T
    python -m api.operator membership revoke --tenant-id T --user-id U --reason T
    python -m api.operator tenant show --tenant-id T

Users are given by --user-id (as returned by GET /v1/me) or by --issuer and
--subject together (for the owner: --owner-*); users are never created here,
so a user must have signed in once. The CLI connects with DATABASE_URL_OPERATOR
as app_operator only, and each command is one transaction: the change and its
operator_audit row are committed together or not at all.

Output: one JSON object on stdout. Exit codes: 0 done or unchanged, 2 usage,
3 refused (nothing changed), 4 database unavailable or misconfigured, or busy
(a lock was not obtained within LOCK_TIMEOUT_MS; nothing changed; retry).
"""

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn, TextIO
from uuid import UUID

from sqlalchemy import Engine, event, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from api.database import (
    DatabaseConfigError,
    DatabaseRoleError,
    DatabaseSettings,
    create_runtime_engine,
    create_session_factory,
)
from api.models import (
    ISSUER_MAX_LENGTH,
    MEMBERSHIP_ROLES,
    REASON_MAX_LENGTH,
    REQUEST_ID_MAX_LENGTH,
    SUBJECT_MAX_LENGTH,
)

OPERATOR_ROLE = "app_operator"
TENANT_NAME_MAX_LENGTH = 200
CONNECT_TIMEOUT_SECONDS = 5
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]+", re.ASCII)
_UNIQUE_VIOLATION = "23505"
_LOCK_NOT_AVAILABLE = "55P03"
# Upper bound for waiting on any lock (the per-tenant advisory lock, a
# concurrent insert of the same request id, row locks). Operator transactions
# hold locks for milliseconds, so waiting longer means something is stuck; the
# command then fails, its transaction rolls back and nothing is changed.
LOCK_TIMEOUT_MS = 5000

EXIT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_UNAVAILABLE = 0, 2, 3, 4


class Busy(Exception):
    """A lock was not obtained within LOCK_TIMEOUT_MS; the transaction rolled back."""


class Refused(Exception):
    """The command was refused and nothing was changed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class UserRef:
    user_id: UUID | None = None
    issuer: str | None = None
    subject: str | None = None


# --- Operations (each runs inside the caller's transaction) --------------------------------


def create_tenant(
    session: Session, *, name: str, owner: UserRef, request_id: str, reason: str
) -> dict[str, Any]:
    """Create a tenant with `owner` as its owner, once per request id.

    Repeating a request id with the same name and owner returns the tenant it
    created; with anything else it is refused.
    """
    _check_text("name", name, TENANT_NAME_MAX_LENGTH)
    _check_request_id(request_id)
    _check_text("reason", reason, REASON_MAX_LENGTH)
    owner_id = _find_user(session, owner)
    previous = session.execute(
        text(
            "SELECT a.tenant_id, a.user_id, t.name FROM operator_audit AS a "
            "JOIN tenants AS t ON t.id = a.tenant_id WHERE a.request_id = :r"
        ),
        {"r": request_id},
    ).one_or_none()
    if previous is not None:
        tenant_id, previous_owner, previous_name = previous
        if (previous_owner, previous_name) != (owner_id, name):
            raise Refused(
                "request_id_conflict", "request id was already used with a different name or owner"
            )
        return {"status": "unchanged", "tenant_id": str(tenant_id), "owner_user_id": str(owner_id)}
    tenant_id = session.execute(
        text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": name}
    ).scalar_one()
    session.execute(
        text("INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, 'owner')"),
        {"t": tenant_id, "u": owner_id},
    )
    _audit(session, "tenant_created", tenant_id, owner_id, None, "owner", reason, request_id)
    return {"status": "created", "tenant_id": str(tenant_id), "owner_user_id": str(owner_id)}


def grant_membership(
    session: Session, *, tenant_id: UUID, user: UserRef, role: str, reason: str
) -> dict[str, Any]:
    """Make the user a member with `role`: insert, change the role, or nothing."""
    if role not in MEMBERSHIP_ROLES:
        raise Refused("invalid_role", "role must be owner, reviewer or member")
    _check_text("reason", reason, REASON_MAX_LENGTH)
    user_id, roles = _lock_tenant_memberships(session, tenant_id, user)
    current = roles.get(user_id)
    result = {"tenant_id": str(tenant_id), "user_id": str(user_id), "role": role}
    if current == role:
        return {"status": "unchanged", **result}
    if current is None:
        session.execute(
            text("INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, :r)"),
            {"t": tenant_id, "u": user_id, "r": role},
        )
        _audit(session, "membership_granted", tenant_id, user_id, None, role, reason)
        return {"status": "granted", **result}
    _refuse_removing_last_owner(roles, user_id)
    session.execute(
        text("UPDATE memberships SET role = :r WHERE tenant_id = :t AND user_id = :u"),
        {"t": tenant_id, "u": user_id, "r": role},
    )
    _audit(session, "membership_role_changed", tenant_id, user_id, current, role, reason)
    return {"status": "role_changed", "previous_role": current, **result}


def revoke_membership(
    session: Session, *, tenant_id: UUID, user: UserRef, reason: str
) -> dict[str, Any]:
    """Remove the user's membership, unless they are the tenant's last owner."""
    _check_text("reason", reason, REASON_MAX_LENGTH)
    user_id, roles = _lock_tenant_memberships(session, tenant_id, user)
    current = roles.get(user_id)
    result = {"tenant_id": str(tenant_id), "user_id": str(user_id)}
    if current is None:
        return {"status": "unchanged", **result}
    _refuse_removing_last_owner(roles, user_id)
    session.execute(
        text("DELETE FROM memberships WHERE tenant_id = :t AND user_id = :u"),
        {"t": tenant_id, "u": user_id},
    )
    _audit(session, "membership_revoked", tenant_id, user_id, current, None, reason)
    return {"status": "revoked", "previous_role": current, **result}


def show_tenant(session: Session, *, tenant_id: UUID) -> dict[str, Any]:
    name = session.execute(
        text("SELECT name FROM tenants WHERE id = :t"), {"t": tenant_id}
    ).scalar_one_or_none()
    if name is None:
        raise Refused("unknown_tenant", "no tenant with this id")
    rows = session.execute(
        text(
            "SELECT user_id, role FROM memberships WHERE tenant_id = :t ORDER BY role DESC, user_id"
        ),
        {"t": tenant_id},
    )
    return {
        "status": "ok",
        "tenant_id": str(tenant_id),
        "name": name,
        "memberships": [{"user_id": str(u), "role": r} for u, r in rows],
    }


def _lock_tenant_memberships(
    session: Session, tenant_id: UUID, user: UserRef
) -> tuple[UUID, dict[UUID, str]]:
    """Serialise membership changes per tenant, then read its memberships.

    A transaction-scoped advisory lock on the tenant makes concurrent changes
    (e.g. two revokes of the two remaining owners) run one after the other, so
    the last-owner check always sees committed state. Row locks on tenants
    would need UPDATE privilege, which app_operator does not have.
    """
    if not isinstance(tenant_id, UUID) or tenant_id.int == 0:
        raise Refused("unknown_tenant", "no tenant with this id")
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:t, 0))"), {"t": str(tenant_id)}
    )
    exists = session.execute(
        text("SELECT 1 FROM tenants WHERE id = :t"), {"t": tenant_id}
    ).scalar_one_or_none()
    if exists is None:
        raise Refused("unknown_tenant", "no tenant with this id")
    user_id = _find_user(session, user)
    rows = session.execute(
        text("SELECT user_id, role FROM memberships WHERE tenant_id = :t"), {"t": tenant_id}
    )
    return user_id, {u: r for u, r in rows}


def _refuse_removing_last_owner(roles: Mapping[UUID, str], user_id: UUID) -> None:
    owners = [u for u, r in roles.items() if r == "owner"]
    if owners == [user_id]:
        raise Refused("last_owner", "the tenant's last owner cannot be removed or demoted")


def _find_user(session: Session, ref: UserRef) -> UUID:
    if ref.user_id is not None:
        found = session.execute(
            text("SELECT id FROM users WHERE id = :u"), {"u": ref.user_id}
        ).scalar_one_or_none()
    else:
        found = session.execute(
            text("SELECT id FROM users WHERE issuer = :i AND subject = :s"),
            {"i": ref.issuer, "s": ref.subject},
        ).scalar_one_or_none()
    if found is None:
        raise Refused("unknown_user", "no such user; the user must sign in once first")
    user_id: UUID = found
    return user_id


def _audit(
    session: Session,
    action: str,
    tenant_id: UUID,
    user_id: UUID,
    old_role: str | None,
    new_role: str | None,
    reason: str,
    request_id: str | None = None,
) -> None:
    session.execute(
        text(
            "INSERT INTO operator_audit "
            "(action, tenant_id, user_id, old_role, new_role, request_id, reason) "
            "VALUES (:action, :tenant_id, :user_id, :old_role, :new_role, :request_id, :reason)"
        ),
        {
            "action": action,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "old_role": old_role,
            "new_role": new_role,
            "request_id": request_id,
            "reason": reason,
        },
    )


def _check_text(field: str, value: str, max_length: int) -> None:
    # Mirrors the database checks: length(btrim(value)) BETWEEN 1 AND max, which
    # trims spaces only; NUL cannot be stored in PostgreSQL text.
    if "\x00" in value or not 1 <= len(value.strip(" ")) <= max_length:
        raise Refused(f"invalid_{field}", f"{field} must be 1 to {max_length} characters")


def _check_request_id(value: str) -> None:
    if len(value) > REQUEST_ID_MAX_LENGTH or _REQUEST_ID.fullmatch(value) is None:
        raise Refused(
            "invalid_request_id",
            f"request id must be 1 to {REQUEST_ID_MAX_LENGTH} of A-Z a-z 0-9 . _ : -",
        )


# --- Connection ------------------------------------------------------------------------------


def create_operator_engine(settings: DatabaseSettings) -> Engine:
    """An engine that connects only as app_operator.

    The runtime guard already refuses superusers, BYPASSRLS and database or
    schema owners (and their members); this also refuses every other role,
    such as app_runtime.
    """
    engine = create_runtime_engine(
        settings, poolclass=NullPool, connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS}
    )

    @event.listens_for(engine, "connect")
    def _require_operator_role(dbapi_connection: Any, _: Any) -> None:
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SELECT current_user, session_user")
            roles = cursor.fetchone()
        dbapi_connection.rollback()
        if tuple(roles) != (OPERATOR_ROLE, OPERATOR_ROLE):
            raise DatabaseRoleError(f"the operator CLI must connect as {OPERATOR_ROLE}")

    return engine


# --- Command line ----------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


class _UsageError(Exception):
    pass


def _uuid(value: str) -> UUID:
    try:
        parsed = UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a UUID") from None
    if parsed.int == 0:
        raise argparse.ArgumentTypeError("must not be the nil UUID")
    return parsed


def _add_user(parser: argparse.ArgumentParser, prefix: str = "") -> None:
    parser.add_argument(f"--{prefix}user-id", type=_uuid)
    parser.add_argument(f"--{prefix}issuer")
    parser.add_argument(f"--{prefix}subject")


def _user_ref(args: argparse.Namespace, prefix: str = "") -> UserRef:
    key = prefix.replace("-", "_")
    user_id = getattr(args, f"{key}user_id")
    issuer = getattr(args, f"{key}issuer")
    subject = getattr(args, f"{key}subject")
    if user_id is not None and issuer is None and subject is None:
        return UserRef(user_id=user_id)
    if user_id is None and issuer and subject:
        # Such values can never match a stored identity (and NUL cannot be sent).
        if (
            len(issuer) > ISSUER_MAX_LENGTH
            or len(subject) > SUBJECT_MAX_LENGTH
            or "\x00" in issuer + subject
        ):
            raise _UsageError(f"--{prefix}issuer or --{prefix}subject is invalid")
        return UserRef(issuer=issuer, subject=subject)
    raise _UsageError(
        f"give either --{prefix}user-id, or --{prefix}issuer and --{prefix}subject together"
    )


def _parser() -> _Parser:
    parser = _Parser(prog="python -m api.operator", description="Tenant and membership admin.")
    groups = parser.add_subparsers(dest="group", required=True, parser_class=_Parser)

    tenant = groups.add_parser("tenant").add_subparsers(
        dest="command", required=True, parser_class=_Parser
    )
    create = tenant.add_parser("create")
    create.add_argument("--name", required=True)
    _add_user(create, "owner-")
    create.add_argument("--request-id", required=True)
    create.add_argument("--reason", required=True)
    show = tenant.add_parser("show")
    show.add_argument("--tenant-id", type=_uuid, required=True)

    membership = groups.add_parser("membership").add_subparsers(
        dest="command", required=True, parser_class=_Parser
    )
    grant = membership.add_parser("grant")
    grant.add_argument("--tenant-id", type=_uuid, required=True)
    _add_user(grant)
    grant.add_argument("--role", choices=MEMBERSHIP_ROLES, required=True)
    grant.add_argument("--reason", required=True)
    revoke = membership.add_parser("revoke")
    revoke.add_argument("--tenant-id", type=_uuid, required=True)
    _add_user(revoke)
    revoke.add_argument("--reason", required=True)
    return parser


def _operation(args: argparse.Namespace) -> Callable[[Session], dict[str, Any]]:
    match (args.group, args.command):
        case ("tenant", "create"):
            owner = _user_ref(args, "owner-")
            return lambda s: create_tenant(
                s, name=args.name, owner=owner, request_id=args.request_id, reason=args.reason
            )
        case ("tenant", "show"):
            return lambda s: show_tenant(s, tenant_id=args.tenant_id)
        case ("membership", "grant"):
            user = _user_ref(args)
            return lambda s: grant_membership(
                s, tenant_id=args.tenant_id, user=user, role=args.role, reason=args.reason
            )
        case ("membership", "revoke"):
            user = _user_ref(args)
            return lambda s: revoke_membership(
                s, tenant_id=args.tenant_id, user=user, reason=args.reason
            )
    raise _UsageError("unknown command")


def run(
    sessions: sessionmaker[Session], operation: Callable[[Session], dict[str, Any]]
) -> dict[str, Any]:
    """Run one operation in one transaction.

    If a concurrent command committed the same tenant request id first, the
    unique constraint rejects ours and the whole transaction rolls back; it is
    then retried once and reports that tenant (or a conflict). Waiting for any
    lock longer than LOCK_TIMEOUT_MS rolls the transaction back and raises
    Busy; that is not retried.
    """
    timeout = int(LOCK_TIMEOUT_MS)
    for attempt in (1, 2):
        try:
            with sessions() as session, session.begin():
                session.execute(text(f"SET LOCAL lock_timeout = {timeout}"))
                return operation(session)
        except DBAPIError as error:
            sqlstate = getattr(error.orig, "sqlstate", None)
            if sqlstate == _LOCK_NOT_AVAILABLE:
                raise Busy from None
            if attempt == 1 and sqlstate == _UNIQUE_VIOLATION:
                continue
            raise
    raise AssertionError("unreachable")


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    env = os.environ if environ is None else environ

    def fail(code: int, error: str, message: str) -> int:
        print(json.dumps({"error": error, "message": message}), file=stderr)
        return code

    try:
        args = _parser().parse_args(argv)
        operation = _operation(args)
    except _UsageError as error:
        return fail(EXIT_USAGE, "usage", str(error))
    try:
        settings = DatabaseSettings.from_env({"DATABASE_URL": env.get("DATABASE_URL_OPERATOR", "")})
    except DatabaseConfigError as error:
        # The message never contains the URL; name the variable the CLI reads.
        return fail(
            EXIT_UNAVAILABLE,
            "misconfigured",
            str(error).replace("DATABASE_URL", "DATABASE_URL_OPERATOR"),
        )
    engine = create_operator_engine(settings)
    try:
        result = run(create_session_factory(engine), operation)
    except Refused as refusal:
        return fail(EXIT_REFUSED, refusal.code, str(refusal))
    except Busy:
        return fail(
            EXIT_UNAVAILABLE,
            "busy",
            "another operation is holding a lock this command needs; nothing was changed, "
            "retry later",
        )
    except DatabaseRoleError as error:
        return fail(EXIT_UNAVAILABLE, "wrong_role", str(error))
    except SQLAlchemyError as error:
        # SQL errors can include connection details; report only the type.
        return fail(EXIT_UNAVAILABLE, "database_error", type(error).__name__)
    finally:
        engine.dispose()
    print(json.dumps(result), file=stdout)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
