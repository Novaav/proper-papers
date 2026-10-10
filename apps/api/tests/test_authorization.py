"""Path ID parsing and the body reader (no database); tenant authorization (app_test)."""

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from api.auth import VerifiedIdentity
from api.authorization import authorize_tenant, parse_resource_id
from api.database import TenantContext, create_session_factory
from api.identity import UserContextMissing, identity_transaction, membership_role
from api.main import MAX_BODY_BYTES, _read_body, _read_document_body
from api.models import DOCUMENT_MAX_BYTES
from tests.conftest import SeededTenant, insert_membership

Seed = Callable[..., SeededTenant]


def test_canonical_lowercase_uuids_are_parsed() -> None:
    value = uuid4()
    assert parse_resource_id(str(value)) == value


@pytest.mark.parametrize(
    "value",
    [
        "00000000-0000-0000-0000-000000000000",
        str(uuid4()).upper(),
        "{" + str(uuid4()) + "}",
        "urn:uuid:" + str(uuid4()),
        uuid4().hex,
        str(uuid4()) + "\n",
        "",
        "not-a-uuid",
    ],
)
def test_non_canonical_or_nil_ids_are_rejected(value: str) -> None:
    assert parse_resource_id(value) is None


def test_non_string_ids_are_rejected() -> None:
    assert parse_resource_id(uuid4()) is None  # type: ignore[arg-type]
    assert parse_resource_id(None) is None  # type: ignore[arg-type]
    assert isinstance(parse_resource_id(str(uuid4())), UUID)


def _request(headers: list[tuple[bytes, bytes]], chunks: list[bytes]) -> tuple[Request, list[int]]:
    reads: list[int] = []

    async def receive() -> dict[str, Any]:
        reads.append(1)
        if not chunks:
            return {"type": "http.request", "body": b"", "more_body": False}
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers, "query_string": b""}
    return Request(scope, receive), reads


def test_declared_oversized_body_is_not_read() -> None:
    request, reads = _request(
        [(b"content-length", str(MAX_BODY_BYTES + 1).encode())], [b"x" * (MAX_BODY_BYTES + 1)]
    )
    assert asyncio.run(_read_body(request)) is None
    assert reads == []


def test_body_reading_stops_once_the_limit_is_exceeded() -> None:
    chunks = [b"x" * 4096] * 10
    request, reads = _request([], list(chunks))
    assert asyncio.run(_read_body(request)) is None
    assert len(reads) == 3  # 4096, 8192, then 12288 > limit


def test_body_within_the_limit_is_returned_whole() -> None:
    request, _ = _request([(b"content-length", b"8")], [b'{"na', b'me"}'])
    assert asyncio.run(_read_body(request)) == b'{"name"}'


def test_upload_body_limit_is_the_document_limit() -> None:
    # Declared over the limit: refused without reading a byte.
    request, reads = _request(
        [(b"content-length", str(DOCUMENT_MAX_BYTES + 1).encode())], [b"%PDF-"]
    )
    assert asyncio.run(_read_document_body(request)) is None
    assert reads == []
    # Streamed without a length: reading stops as soon as the limit is passed.
    chunk = b"x" * (1 << 20)
    chunks = [chunk] * 25
    request, reads = _request([], list(chunks))
    assert asyncio.run(_read_document_body(request)) is None
    assert len(reads) == DOCUMENT_MAX_BYTES // len(chunk) + 1
    # Exactly the limit is returned whole.
    request, _ = _request([], [chunk] * (DOCUMENT_MAX_BYTES // len(chunk)))
    assert len(asyncio.run(_read_document_body(request)) or b"") == DOCUMENT_MAX_BYTES


# --- authorize_tenant and membership_role, as app_runtime on app_test --------------------


def _identity() -> VerifiedIdentity:
    return VerifiedIdentity("https://issuer.test/", f"auth0|authz-{uuid4()}")


def _grant(
    owner_engine: Engine,
    sessions: sessionmaker[Session],
    identity: VerifiedIdentity,
    tenant_id: UUID,
    role: str,
) -> UUID:
    with identity_transaction(sessions, identity) as current:
        user_id = current.user_id
    with owner_engine.begin() as connection:
        insert_membership(connection, tenant_id, user_id, role)
    return user_id


@pytest.mark.parametrize("role", ["owner", "member"])
def test_members_get_a_context_with_their_role(
    runtime_engine: Engine, owner_engine: Engine, seed_tenant: Seed, role: str
) -> None:
    sessions = create_session_factory(runtime_engine)
    tenant_id = seed_tenant().ctx.tenant_id
    identity = _identity()
    user_id = _grant(owner_engine, sessions, identity, tenant_id, role)

    assert authorize_tenant(sessions, identity, tenant_id) == TenantContext(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,  # type: ignore[arg-type]
    )


def test_non_members_and_unknown_tenants_get_no_context(
    runtime_engine: Engine, owner_engine: Engine, seed_tenant: Seed
) -> None:
    sessions = create_session_factory(runtime_engine)
    a, b = seed_tenant(), seed_tenant()
    identity = _identity()
    _grant(owner_engine, sessions, identity, a.ctx.tenant_id, "owner")

    assert authorize_tenant(sessions, identity, b.ctx.tenant_id) is None
    assert authorize_tenant(sessions, identity, uuid4()) is None
    # Another user's membership in the same tenant (b's seeded owner) never counts.
    assert authorize_tenant(sessions, _identity(), a.ctx.tenant_id) is None


def test_membership_role_requires_a_user_and_a_uuid(runtime_engine: Engine) -> None:
    sessions = create_session_factory(runtime_engine)
    with sessions() as session, session.begin(), pytest.raises(UserContextMissing):
        membership_role(session, uuid4())
    with identity_transaction(sessions, _identity()) as current, pytest.raises(TypeError):
        membership_role(current.session, str(uuid4()))  # type: ignore[arg-type]
