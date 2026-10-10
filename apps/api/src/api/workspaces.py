"""Workspace repository, scoped to the tenant of the current transaction.

Every function takes the Session yielded by `tenant_transaction` and runs inside
its transaction: nothing here creates engines, sessions or transactions, or
commits. The tenant is never a parameter; it comes from the transaction-local
`app.tenant_id` setting, and row-level security limits every statement to it
and to tenants the transaction's user (`app.user_id`) is a member of.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Uuid, func, insert, select
from sqlalchemy.orm import Session

from api.models import Workspace

# Mirrors ck_workspaces_name_length: length(btrim(name)) BETWEEN 1 AND 200.
# btrim() without arguments trims only spaces, and length() counts characters.
NAME_MIN_LENGTH = 1
NAME_MAX_LENGTH = 200

# The tenant and user set by tenant_transaction(); NULL when the transaction has none.
_CURRENT_TENANT = func.nullif(func.current_setting("app.tenant_id", True), "").cast(Uuid())
_CURRENT_USER = func.nullif(func.current_setting("app.user_id", True), "").cast(Uuid())


class TenantContextMissing(Exception):
    """A repository function ran outside tenant_transaction()."""


class InvalidWorkspaceName(ValueError):
    """The name would violate the workspace name constraint."""


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    id: UUID
    tenant_id: UUID
    name: str
    created_at: datetime


_COLUMNS = (Workspace.id, Workspace.tenant_id, Workspace.name, Workspace.created_at)


def create_workspace(session: Session, name: str) -> WorkspaceRecord:
    """Insert a workspace for the current tenant. The caller's transaction commits it."""
    _validate_name(name)
    _require_tenant(session)
    row = session.execute(
        insert(Workspace).values(tenant_id=_CURRENT_TENANT, name=name).returning(*_COLUMNS)
    ).one()
    return WorkspaceRecord(*row)


def list_workspaces(session: Session) -> list[WorkspaceRecord]:
    """The current tenant's workspaces, oldest first (ties broken by id)."""
    _require_tenant(session)
    rows = session.execute(select(*_COLUMNS).order_by(Workspace.created_at, Workspace.id))
    return [WorkspaceRecord(*row) for row in rows]


def get_workspace(session: Session, workspace_id: UUID) -> WorkspaceRecord | None:
    """The workspace if the current tenant can see it; None if it is missing or
    belongs to another tenant, which are deliberately indistinguishable."""
    if not isinstance(workspace_id, UUID):
        raise TypeError("workspace_id must be a uuid.UUID")
    _require_tenant(session)
    row = session.execute(select(*_COLUMNS).where(Workspace.id == workspace_id)).one_or_none()
    return None if row is None else WorkspaceRecord(*row)


def _require_tenant(session: Session) -> None:
    # Fails closed instead of silently returning nothing when called outside
    # tenant_transaction(); a malformed setting raises in the database.
    tenant, user = session.execute(select(_CURRENT_TENANT, _CURRENT_USER)).one()
    if tenant is None or user is None:
        raise TenantContextMissing("repository functions must run inside tenant_transaction()")


def _validate_name(name: str) -> None:
    if not isinstance(name, str):
        raise TypeError("name must be a str")
    if "\x00" in name:
        raise InvalidWorkspaceName("name must not contain NUL characters")
    if not NAME_MIN_LENGTH <= len(name.strip(" ")) <= NAME_MAX_LENGTH:
        raise InvalidWorkspaceName(
            f"name must be {NAME_MIN_LENGTH} to {NAME_MAX_LENGTH} characters, "
            "not counting leading and trailing spaces"
        )
