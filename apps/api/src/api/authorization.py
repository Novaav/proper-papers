"""Tenant authorization: the only place a TenantContext is created.

A request may act in a tenant only if its verified user is a member of it.
authorize_tenant() checks that in its own transaction and returns the context
for the next one; row-level security re-checks the membership on every
workspace statement, so a membership revoked in between yields nothing.
"""

import re
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from api.auth import VerifiedIdentity
from api.database import TenantContext
from api.identity import identity_transaction, membership_role

# The canonical lowercase 8-4-4-4-12 form only, as the API itself writes IDs.
# uuid.UUID() also accepts upper case, braces, "urn:uuid:" and unhyphenated
# forms, which would give one resource several URLs.
_CANONICAL_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.ASCII
)


def parse_resource_id(value: str) -> UUID | None:
    """A path identifier as a UUID, or None when it is not one (then: not found)."""
    if not isinstance(value, str) or _CANONICAL_UUID.fullmatch(value) is None:
        return None
    parsed = UUID(value)
    return None if parsed.int == 0 else parsed


def authorize_tenant(
    sessions: sessionmaker[Session], identity: VerifiedIdentity, tenant_id: UUID
) -> TenantContext | None:
    """The caller's context in `tenant_id`, or None if they are not a member.

    An unknown tenant and a tenant the caller does not belong to are
    deliberately indistinguishable. Provisions the user on first sight, like
    GET /v1/me.
    """
    with identity_transaction(sessions, identity) as current:
        role = membership_role(current.session, tenant_id)
        user_id = current.user_id
    if role is None:
        return None
    return TenantContext(user_id=user_id, tenant_id=tenant_id, role=role)
