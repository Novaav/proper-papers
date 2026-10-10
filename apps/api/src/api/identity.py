"""The current user, from a verified token, inside one database transaction.

identity_transaction() sets the transaction-local app.auth_issuer and
app.auth_subject from a VerifiedIdentity, provisions the user on first sight
(row-level security allows only that identity), and sets app.user_id. Like
tenant_transaction(), everything ends with the transaction.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Uuid, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from api.auth import VerifiedIdentity
from api.database import TenantRole
from api.models import Membership, User

_CURRENT_USER = func.nullif(func.current_setting("app.user_id", True), "").cast(Uuid())


class UserContextMissing(Exception):
    """A user-scoped query ran outside identity_transaction()."""


@dataclass(frozen=True, slots=True)
class CurrentUser:
    session: Session
    user_id: UUID


@dataclass(frozen=True, slots=True)
class MembershipRecord:
    tenant_id: UUID
    role: TenantRole


@contextmanager
def identity_transaction(
    sessions: sessionmaker[Session], identity: VerifiedIdentity
) -> Iterator[CurrentUser]:
    """Run one transaction as the verified identity; provisions the user if new."""
    if not isinstance(identity, VerifiedIdentity):
        raise TypeError("identity_transaction requires a VerifiedIdentity")
    with sessions() as session, session.begin():
        session.execute(
            text(
                "SELECT set_config('app.auth_issuer', :issuer, true), "
                "set_config('app.auth_subject', :subject, true)"
            ),
            {"issuer": identity.issuer, "subject": identity.subject},
        )
        # Idempotent and safe under concurrency (see test_identity_schema).
        user_id = session.execute(
            insert(User)
            .values(issuer=identity.issuer, subject=identity.subject)
            .on_conflict_do_nothing(index_elements=["issuer", "subject"])
            .returning(User.id)
        ).scalar_one_or_none()
        if user_id is None:
            user_id = session.execute(
                select(User.id).where(
                    User.issuer == identity.issuer, User.subject == identity.subject
                )
            ).scalar_one()
        session.execute(
            text("SELECT set_config('app.user_id', :user_id, true)"), {"user_id": str(user_id)}
        )
        yield CurrentUser(session=session, user_id=user_id)


def list_memberships(session: Session) -> list[MembershipRecord]:
    """The current user's memberships, ordered by tenant id; RLS hides all others."""
    if session.execute(select(_CURRENT_USER)).scalar_one() is None:
        raise UserContextMissing("list_memberships must run inside identity_transaction()")
    rows = session.execute(
        select(Membership.tenant_id, Membership.role).order_by(Membership.tenant_id)
    )
    return [MembershipRecord(tenant_id=tenant_id, role=_role(role)) for tenant_id, role in rows]


def _role(value: str) -> TenantRole:
    # ck_memberships_role guarantees one of these; anything else is a schema bug.
    if value == "owner":
        return "owner"
    if value == "reviewer":
        return "reviewer"
    if value == "member":
        return "member"
    raise ValueError("unexpected membership role")


def membership_role(session: Session, tenant_id: UUID) -> TenantRole | None:
    """The current user's role in `tenant_id`, or None if they are not a member.

    Row-level security shows only the current user's memberships, so another
    user's membership in the tenant never counts.
    """
    if not isinstance(tenant_id, UUID):
        raise TypeError("tenant_id must be a uuid.UUID")
    current_user = session.execute(select(_CURRENT_USER)).scalar_one()
    if current_user is None:
        raise UserContextMissing("membership_role must run inside identity_transaction()")
    role = session.execute(
        select(Membership.role).where(
            Membership.tenant_id == tenant_id, Membership.user_id == current_user
        )
    ).scalar_one_or_none()
    return None if role is None else _role(role)
