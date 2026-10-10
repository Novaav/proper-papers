"""Document endpoints end to end: real app, PostgreSQL 18 (app_test), local issuer."""

import asyncio
import hashlib
import logging
from collections.abc import Callable, Iterator, MutableMapping
from datetime import timedelta
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from api import documents, main
from api.authorization import authorize_tenant
from api.database import DatabaseSettings
from api.main import create_app
from api.models import DOCUMENT_MAX_BYTES
from api.workspaces import get_workspace
from tests.conftest import SeededTenant
from tests.fake_clamd import FakeClamd, fresh_version
from tests.test_workspace_endpoints import (  # noqa: F401
    Api,
    _environ,
    _fetcher,
    _subject,
    api,
    issuer,
)

Seed = Callable[..., SeededTenant]
PDF = b"%PDF-1.7\n% synthetic test document, never parsed\n%%EOF\n"
NOT_FOUND = {"detail": "not_found"}


def _docs(tenant: object, workspace: object) -> str:
    return f"/v1/tenants/{tenant}/workspaces/{workspace}/documents"


def upload(
    api: Api,  # noqa: F811
    tenant: object,
    workspace: object,
    subject: str | None,
    content: bytes | Iterator[bytes] = PDF,
    name: str | None = "contract.pdf",
    content_type: str = "application/pdf",
    extra: dict[str, str] | None = None,
) -> httpx.Response:
    headers = {} if subject is None else api.headers(subject)
    headers["Content-Type"] = content_type
    if name is not None:
        headers["X-Document-Name"] = quote(name, safe="")
    headers.update(extra or {})
    return api.request("POST", _docs(tenant, workspace), None, headers=headers, content=content)


def _counts(owner_engine: Engine, tenant_id: UUID) -> tuple[int, int, int, int]:
    """Rows of the tenant in documents, versions, contents and scans."""
    with owner_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT (SELECT count(*) FROM documents WHERE tenant_id = :t), "
                "(SELECT count(*) FROM document_versions WHERE tenant_id = :t), "
                "(SELECT count(*) FROM document_contents WHERE tenant_id = :t), "
                "(SELECT count(*) FROM document_scans WHERE tenant_id = :t)"
            ),
            {"t": tenant_id},
        ).one()
    return row[0], row[1], row[2], row[3]


@pytest.fixture
def member(api: Api, seed_tenant: Seed) -> tuple[SeededTenant, str]:  # noqa: F811
    tenant = seed_tenant()
    subject = _subject()
    api.grant(subject, tenant.ctx.tenant_id)
    return tenant, subject


# --- happy path ---------------------------------------------------------------------------------


def test_upload_list_get_and_download_round_trip(
    api: Api,  # noqa: F811
    member: tuple[SeededTenant, str],
) -> None:
    tenant, alice = member
    t, w = tenant.ctx.tenant_id, tenant.workspace_id

    created = upload(api, t, w, alice, name="Årsredovisning 2025.pdf")
    assert created.status_code == 201
    body = created.json()
    assert set(body) == {"id", "workspace_id", "name", "created_at", "version"}
    assert body["name"] == "Årsredovisning 2025.pdf"
    assert body["workspace_id"] == str(w)
    assert body["version"]["number"] == 1
    assert body["version"]["media_type"] == "application/pdf"
    assert body["version"]["size_bytes"] == len(PDF)
    assert body["version"]["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert created.headers["location"] == f"{_docs(t, w)}/{body['id']}"
    assert created.headers["cache-control"] == "no-store"

    listed = api.request("GET", _docs(t, w), alice)
    assert (listed.status_code, listed.json()) == (200, {"documents": [body]})
    got = api.request("GET", f"{_docs(t, w)}/{body['id']}", alice)
    assert (got.status_code, got.json()) == (200, body)

    download = api.request("GET", f"{_docs(t, w)}/{body['id']}/content", alice)
    assert download.status_code == 200
    assert download.content == PDF
    assert hashlib.sha256(download.content).hexdigest() == body["version"]["sha256"]
    assert download.headers["content-type"] == "application/pdf"
    assert download.headers["content-length"] == str(len(PDF))
    assert download.headers["content-disposition"] == (
        'attachment; filename="_rsredovisning 2025.pdf"; '
        "filename*=UTF-8''%C3%85rsredovisning%202025.pdf"
    )
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert download.headers["cache-control"] == "no-store"
    assert "accept-ranges" not in download.headers


def test_octet_stream_with_pdf_bytes_is_accepted_and_stored_as_pdf(
    api: Api,  # noqa: F811
    member: tuple[SeededTenant, str],
) -> None:
    tenant, alice = member
    created = upload(
        api,
        tenant.ctx.tenant_id,
        tenant.workspace_id,
        alice,
        content_type="application/octet-stream",
    )
    assert (created.status_code, created.json()["version"]["media_type"]) == (
        201,
        "application/pdf",
    )


def test_path_parts_in_names_are_dropped(api: Api, member: tuple[SeededTenant, str]) -> None:  # noqa: F811
    tenant, alice = member
    created = upload(
        api, tenant.ctx.tenant_id, tenant.workspace_id, alice, name="../../etc/passwd.pdf"
    )
    assert (created.status_code, created.json()["name"]) == (201, "passwd.pdf")


# --- limits, types, names -----------------------------------------------------------------------


def test_exactly_the_size_limit_is_accepted(api: Api, member: tuple[SeededTenant, str]) -> None:  # noqa: F811
    tenant, alice = member
    content = b"%PDF-" + b"x" * (DOCUMENT_MAX_BYTES - 5)
    created = upload(api, tenant.ctx.tenant_id, tenant.workspace_id, alice, content=content)
    assert created.status_code == 201
    download = api.request(
        "GET",
        f"{_docs(tenant.ctx.tenant_id, tenant.workspace_id)}/{created.json()['id']}/content",
        alice,
    )
    assert hashlib.sha256(download.content).digest() == hashlib.sha256(content).digest()


@pytest.mark.parametrize("streamed", [False, True], ids=["declared-length", "streamed"])
def test_one_byte_over_the_limit_is_413_and_nothing_is_stored(
    api: Api,  # noqa: F811
    owner_engine: Engine,
    member: tuple[SeededTenant, str],
    streamed: bool,
) -> None:
    tenant, alice = member
    content = b"%PDF-" + b"x" * (DOCUMENT_MAX_BYTES - 4)

    def chunks() -> Iterator[bytes]:
        for start in range(0, len(content), 1 << 20):
            yield content[start : start + (1 << 20)]

    response = upload(
        api,
        tenant.ctx.tenant_id,
        tenant.workspace_id,
        alice,
        content=chunks() if streamed else content,
    )
    assert (response.status_code, response.json()) == (413, {"detail": "too_large"})
    assert _counts(owner_engine, tenant.ctx.tenant_id) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"", "application/pdf"),
        (b"not a pdf", "application/pdf"),
        (b"<html><body>%PDF-1.7</body></html>", "application/pdf"),
        (PDF, "text/html"),
        (PDF, "image/png"),
        (PDF, ""),
    ],
    ids=["empty", "text", "html-with-signature-later", "pdf-as-html", "pdf-as-png", "no-type"],
)
def test_non_pdfs_and_untrusted_declared_types_are_415(
    api: Api,  # noqa: F811
    owner_engine: Engine,
    member: tuple[SeededTenant, str],
    content: bytes,
    content_type: str,
) -> None:
    tenant, alice = member
    response = upload(
        api,
        tenant.ctx.tenant_id,
        tenant.workspace_id,
        alice,
        content=content,
        content_type=content_type,
    )
    assert (response.status_code, response.json()) == (415, {"detail": "unsupported_media_type"})
    assert _counts(owner_engine, tenant.ctx.tenant_id) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    "extra",
    [
        {"X-Document-Name": "%FF%FE.pdf"},
        {"X-Document-Name": quote("a‮fdp.exe", safe="")},
        {"X-Document-Name": quote("x" * 256, safe="")},
        {"X-Document-Name": "%00.pdf"},
        {"X-Document-Name": ""},
    ],
    ids=["invalid-utf8", "rtl-override", "too-long", "nul", "empty"],
)
def test_invalid_names_are_422_and_nothing_is_stored(
    api: Api,  # noqa: F811
    owner_engine: Engine,
    member: tuple[SeededTenant, str],
    extra: dict[str, str],
) -> None:
    tenant, alice = member
    response = upload(api, tenant.ctx.tenant_id, tenant.workspace_id, alice, name=None, extra=extra)
    assert (response.status_code, response.json()) == (422, {"detail": "invalid_name"})
    assert _counts(owner_engine, tenant.ctx.tenant_id) == (0, 0, 0, 0)


def test_missing_name_header_is_422(api: Api, member: tuple[SeededTenant, str]) -> None:  # noqa: F811
    tenant, alice = member
    response = upload(api, tenant.ctx.tenant_id, tenant.workspace_id, alice, name=None)
    assert (response.status_code, response.json()) == (422, {"detail": "invalid_name"})


# --- isolation ----------------------------------------------------------------------------------


def test_other_tenants_cannot_see_or_add_documents(
    api: Api,  # noqa: F811
    owner_engine: Engine,
    seed_tenant: Seed,
) -> None:
    a, b = seed_tenant(), seed_tenant()
    alice, bob = _subject(), _subject()
    api.grant(alice, a.ctx.tenant_id)
    api.grant(bob, b.ctx.tenant_id)
    doc = upload(api, a.ctx.tenant_id, a.workspace_id, alice).json()["id"]
    ta, wa, tb, wb = a.ctx.tenant_id, a.workspace_id, b.ctx.tenant_id, b.workspace_id

    responses = [
        # Bob, through A's tenant path: not a member.
        api.request("GET", _docs(ta, wa), bob),
        api.request("GET", f"{_docs(ta, wa)}/{doc}", bob),
        api.request("GET", f"{_docs(ta, wa)}/{doc}/content", bob),
        upload(api, ta, wa, bob),
        # Bob, A's workspace or document under his own tenant path.
        api.request("GET", _docs(tb, wa), bob),
        api.request("GET", f"{_docs(tb, wa)}/{doc}/content", bob),
        api.request("GET", f"{_docs(tb, wb)}/{doc}", bob),
        api.request("GET", f"{_docs(tb, wb)}/{doc}/content", bob),
        upload(api, tb, wa, bob),
    ]
    assert [(r.status_code, r.json()) for r in responses] == [(404, NOT_FOUND)] * len(responses)
    assert _counts(owner_engine, ta) == (1, 1, 1, 1)
    assert _counts(owner_engine, tb) == (0, 0, 0, 0)


def test_a_document_is_found_only_under_its_own_workspace(
    api: Api,  # noqa: F811
    owner_engine: Engine,
    member: tuple[SeededTenant, str],
) -> None:
    tenant, alice = member
    with owner_engine.begin() as connection:
        other: UUID = connection.execute(
            text("INSERT INTO workspaces (tenant_id, name) VALUES (:t, 'Other') RETURNING id"),
            {"t": tenant.ctx.tenant_id},
        ).scalar_one()
    doc = upload(api, tenant.ctx.tenant_id, tenant.workspace_id, alice).json()["id"]

    for path in (
        f"{_docs(tenant.ctx.tenant_id, other)}/{doc}",
        f"{_docs(tenant.ctx.tenant_id, other)}/{doc}/content",
    ):
        assert api.request("GET", path, alice).status_code == 404
    assert api.request("GET", _docs(tenant.ctx.tenant_id, other), alice).json() == {"documents": []}


@pytest.mark.parametrize(
    "malformed", ["not-a-uuid", "00000000-0000-0000-0000-000000000000", str(uuid4()).upper()]
)
def test_malformed_and_unknown_ids_are_404(
    api: Api,  # noqa: F811
    member: tuple[SeededTenant, str],
    malformed: str,
) -> None:
    tenant, alice = member
    t, w = tenant.ctx.tenant_id, tenant.workspace_id
    for response in (
        api.request("GET", _docs(t, malformed), alice),
        api.request("GET", f"{_docs(t, w)}/{malformed}", alice),
        api.request("GET", f"{_docs(t, w)}/{malformed}/content", alice),
        api.request("GET", f"{_docs(t, w)}/{uuid4()}/content", alice),
        upload(api, t, malformed, alice),
        upload(api, t, uuid4(), alice),
    ):
        assert (response.status_code, response.json()) == (404, NOT_FOUND)


def test_download_is_authorized_on_every_request(
    api: Api,  # noqa: F811
    member: tuple[SeededTenant, str],
) -> None:
    tenant, alice = member
    t, w = tenant.ctx.tenant_id, tenant.workspace_id
    doc = upload(api, t, w, alice).json()["id"]
    path = f"{_docs(t, w)}/{doc}/content"
    assert api.request("GET", path, alice).status_code == 200

    api.revoke(alice, t)

    assert (
        api.request("GET", path, alice).status_code,
        api.request("GET", path, alice).json(),
    ) == (404, NOT_FOUND)
    assert api.request("GET", path, None).status_code == 401


def test_membership_revoked_after_authorization_stores_nothing(
    api: Api,  # noqa: F811
    owner_engine: Engine,
    member: tuple[SeededTenant, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, alice = member
    real = authorize_tenant

    def authorize_then_revoke(*args: Any, **kwargs: Any) -> Any:
        ctx = real(*args, **kwargs)
        with owner_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM memberships WHERE tenant_id = :t"), {"t": tenant.ctx.tenant_id}
            )
        return ctx

    monkeypatch.setattr(main, "authorize_tenant", authorize_then_revoke)
    response = upload(api, tenant.ctx.tenant_id, tenant.workspace_id, alice)
    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert _counts(owner_engine, tenant.ctx.tenant_id) == (0, 0, 0, 0)


def test_membership_revoked_after_the_pre_read_check_stores_nothing(
    api: Api,  # noqa: F811
    owner_engine: Engine,
    member: tuple[SeededTenant, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Revoked after the upload was authorized and its body read: the insert's
    # own row-level security check still refuses it (404, nothing stored).
    tenant, alice = member
    real = get_workspace

    def check_then_revoke(*args: Any, **kwargs: Any) -> Any:
        record = real(*args, **kwargs)
        with owner_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM memberships WHERE tenant_id = :t"), {"t": tenant.ctx.tenant_id}
            )
        return record

    monkeypatch.setattr(main, "get_workspace", check_then_revoke)
    response = upload(api, tenant.ctx.tenant_id, tenant.workspace_id, alice)
    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert _counts(owner_engine, tenant.ctx.tenant_id) == (0, 0, 0, 0)


def test_membership_revoked_just_before_the_insert_is_404_and_stores_nothing(
    api: Api,  # noqa: F811
    owner_engine: Engine,
    member: tuple[SeededTenant, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Revoked after every check but before the insert: row-level security
    # rejects the insert itself, which must still be the uniform 404.
    tenant, alice = member
    real = documents._require_workspace

    def check_then_revoke(*args: Any, **kwargs: Any) -> None:
        real(*args, **kwargs)
        with owner_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM memberships WHERE tenant_id = :t"), {"t": tenant.ctx.tenant_id}
            )

    monkeypatch.setattr(documents, "_require_workspace", check_then_revoke)
    response = upload(api, tenant.ctx.tenant_id, tenant.workspace_id, alice)
    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert _counts(owner_engine, tenant.ctx.tenant_id) == (0, 0, 0, 0)


# --- authorization before the body is read (D-058) ---------------------------------------------


def _asgi_upload(
    api: Api,  # noqa: F811
    path: str,
    headers: dict[str, str],
    chunks: list[bytes],
) -> tuple[int, int]:
    """POSTs to the app directly over ASGI; returns (status, body reads).

    Every call of `receive` counts as a read, so 0 proves the app never asked
    for any of the body.
    """
    reads = 0
    pending = list(chunks)
    status: list[int] = []

    async def receive() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        if not pending:
            return {"type": "http.request", "body": b"", "more_body": False}
        chunk = pending.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    asyncio.run(api.client.app(scope, receive, send))
    return status[0], reads


def _body_headers(api: Api, subject: str | None, declared: int | None) -> dict[str, str]:  # noqa: F811
    headers = {} if subject is None else api.headers(subject)
    headers.update({"Content-Type": "application/pdf", "X-Document-Name": "contract.pdf"})
    if declared is not None:
        headers["Content-Length"] = str(declared)
    return headers


# 2 MiB in two chunks; declared (Content-Length) and streamed without a length.
CHUNKS = [PDF + b"x" * (1 << 20), b"x" * (1 << 20)]
DECLARED = sum(len(chunk) for chunk in CHUNKS)


@pytest.mark.parametrize("declared", [DECLARED, None], ids=["content-length", "no-length"])
def test_authorized_upload_reads_the_body(
    api: Api,  # noqa: F811
    member: tuple[SeededTenant, str],
    declared: int | None,
) -> None:
    # Positive control for the counter: an authorized upload reads every chunk.
    tenant, alice = member
    status, reads = _asgi_upload(
        api,
        _docs(tenant.ctx.tenant_id, tenant.workspace_id),
        _body_headers(api, alice, declared),
        list(CHUNKS),
    )
    assert status == 201
    assert reads >= len(CHUNKS)


@pytest.mark.parametrize("declared", [DECLARED, None], ids=["content-length", "no-length"])
def test_unauthorized_uploads_are_refused_before_any_body_read(
    api: Api,  # noqa: F811
    owner_engine: Engine,
    seed_tenant: Seed,
    member: tuple[SeededTenant, str],
    declared: int | None,
) -> None:
    tenant, alice = member
    other = seed_tenant()  # alice is not a member here
    cases = [
        ("non-member tenant", other.ctx.tenant_id, other.workspace_id, alice, 404),
        ("unknown workspace", tenant.ctx.tenant_id, uuid4(), alice, 404),
        ("workspace of another tenant", tenant.ctx.tenant_id, other.workspace_id, alice, 404),
        ("malformed tenant id", "not-a-uuid", tenant.workspace_id, alice, 404),
        ("malformed workspace id", tenant.ctx.tenant_id, "not-a-uuid", alice, 404),
        ("no token", tenant.ctx.tenant_id, tenant.workspace_id, None, 401),
    ]
    for label, tenant_id, workspace_id, subject, expected in cases:
        status, reads = _asgi_upload(
            api, _docs(tenant_id, workspace_id), _body_headers(api, subject, declared), list(CHUNKS)
        )
        assert (label, status, reads) == (label, expected, 0)
    invalid = _body_headers(api, None, declared) | {"Authorization": "Bearer not-a-jwt"}
    status, reads = _asgi_upload(
        api, _docs(tenant.ctx.tenant_id, tenant.workspace_id), invalid, list(CHUNKS)
    )
    assert (status, reads) == (401, 0)
    for seeded in (tenant, other):
        assert _counts(owner_engine, seeded.ctx.tenant_id) == (0, 0, 0, 0)


# Invalid uploads: (label, header changes, body chunks, code when the workspace is visible).
INVALID_UPLOADS: list[tuple[str, dict[str, str | None], list[bytes], int]] = [
    ("declared oversize", {"Content-Length": str(DOCUMENT_MAX_BYTES + 1)}, [PDF], 413),
    ("streamed oversize", {}, [PDF + b"x" * (1 << 20)] + [b"x" * (1 << 20)] * 20, 413),
    ("untrusted declared type", {"Content-Type": "text/html"}, [PDF], 415),
    ("bytes are not a PDF", {}, [b"<html>not a pdf</html>"], 415),
    ("missing name", {"X-Document-Name": None}, [PDF], 422),
    ("control character in name", {"X-Document-Name": "a%00b.pdf"}, [PDF], 422),
]


def _invalid_headers(
    api: Api,  # noqa: F811
    subject: str,
    changes: dict[str, str | None],
) -> dict[str, str]:
    headers = _body_headers(api, subject, None)
    for key, value in changes.items():
        if value is None:
            headers.pop(key)
        else:
            headers[key] = value
    return headers


def test_an_invisible_workspace_is_404_before_any_body_validation(
    api: Api,  # noqa: F811
    owner_engine: Engine,
    seed_tenant: Seed,
    member: tuple[SeededTenant, str],
    fake_clamd: FakeClamd,
) -> None:
    # Intended error precedence since D-058: whether the workspace is visible is
    # decided before the body is read, so an unknown, foreign or non-member
    # workspace is 404 even when the upload is also oversized, not a PDF or
    # badly named. (Before D-058 a member's unknown workspace in their own
    # tenant got 413/415/422 first.) Status codes and bodies are unchanged.
    tenant, alice = member
    other = seed_tenant()
    targets = [
        ("unknown workspace", tenant.ctx.tenant_id, uuid4()),
        ("another tenant's workspace", tenant.ctx.tenant_id, other.workspace_id),
        ("non-member tenant", other.ctx.tenant_id, other.workspace_id),
    ]
    for target, tenant_id, workspace_id in targets:
        for label, changes, chunks, _ in INVALID_UPLOADS:
            status, reads = _asgi_upload(
                api, _docs(tenant_id, workspace_id), _invalid_headers(api, alice, changes), chunks
            )
            assert (target, label, status, reads) == (target, label, 404, 0)
    # Control: the same uploads to a visible workspace keep their own codes.
    for label, changes, chunks, expected in INVALID_UPLOADS:
        status, _ = _asgi_upload(
            api,
            _docs(tenant.ctx.tenant_id, tenant.workspace_id),
            _invalid_headers(api, alice, changes),
            chunks,
        )
        assert (label, status) == (label, expected)
    # Invalid or unauthorized uploads never reach the malware scanner.
    assert fake_clamd.streams == []
    for seeded in (tenant, other):
        assert _counts(owner_engine, seeded.ctx.tenant_id) == (0, 0, 0, 0)


# --- malware scanning ---------------------------------------------------------------------------


def test_a_clean_upload_is_stored_with_its_scan_and_downloadable(
    api: Api,  # noqa: F811
    owner_engine: Engine,
    member: tuple[SeededTenant, str],
    fake_clamd: FakeClamd,
) -> None:
    tenant, alice = member
    response = upload(api, tenant.ctx.tenant_id, tenant.workspace_id, alice)
    assert response.status_code == 201
    assert response.json()["version"]["scan_status"] == "clean"
    assert fake_clamd.streams == [PDF]  # exactly the uploaded bytes were scanned
    with owner_engine.connect() as connection:
        scan = connection.execute(
            text(
                "SELECT scan_number, verdict, engine, engine_version, signature_version "
                "FROM document_scans WHERE tenant_id = :t"
            ),
            {"t": tenant.ctx.tenant_id},
        ).one()
    assert tuple(scan) == (1, "clean", "ClamAV", "1.4.6", "27777")
    listed = api.request("GET", _docs(tenant.ctx.tenant_id, tenant.workspace_id), alice)
    assert listed.json()["documents"][0]["version"]["scan_status"] == "clean"
    content = api.request("GET", f"{response.headers['location']}/content", alice)
    assert (content.status_code, content.content) == (200, PDF)


@pytest.mark.parametrize(
    ("reply", "status", "detail"),
    [
        (b"stream: Eicar-Test-Signature FOUND", 422, "malware_detected"),
        (b"stream: Heuristics.Limits.Exceeded.MaxScanSize FOUND", 422, "scan_incomplete"),
        (b"stream: Heuristics.Encrypted.PDF FOUND", 422, "encrypted_pdf"),
        (b"stream: Can't allocate memory ERROR", 503, "scan_unavailable"),
        (b"stream: something unexpected", 503, "scan_unavailable"),
        (None, 503, "scan_unavailable"),  # closed without a verdict
    ],
)
def test_every_non_clean_scan_outcome_stores_nothing(
    api: Api,  # noqa: F811
    owner_engine: Engine,
    member: tuple[SeededTenant, str],
    fake_clamd: FakeClamd,
    reply: bytes | None,
    status: int,
    detail: str,
) -> None:
    tenant, alice = member
    fake_clamd.scan_reply = reply
    response = upload(api, tenant.ctx.tenant_id, tenant.workspace_id, alice)
    assert (response.status_code, response.json()) == (status, {"detail": detail})
    assert fake_clamd.streams == [PDF]
    assert _counts(owner_engine, tenant.ctx.tenant_id) == (0, 0, 0, 0)


def test_stale_or_unknown_signatures_are_503_and_nothing_is_scanned(
    api: Api,  # noqa: F811
    owner_engine: Engine,
    member: tuple[SeededTenant, str],
    fake_clamd: FakeClamd,
) -> None:
    tenant, alice = member
    for version in (fresh_version(age=timedelta(hours=73)), b"COMMAND UNAVAILABLE"):
        fake_clamd.version_reply = version
        response = upload(api, tenant.ctx.tenant_id, tenant.workspace_id, alice)
        assert (response.status_code, response.json()) == (503, {"detail": "scan_unavailable"})
    assert fake_clamd.streams == []
    assert _counts(owner_engine, tenant.ctx.tenant_id) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    "marker",
    [b"trailer\n<< /Size 3 /Encrypt 5 0 R >>", b"<< /Encr#79pt 5 0 R /Type /XRef >>"],
)
def test_an_encryption_marker_is_422_before_the_scanner_is_asked(
    api: Api,  # noqa: F811
    owner_engine: Engine,
    member: tuple[SeededTenant, str],
    fake_clamd: FakeClamd,
    marker: bytes,
) -> None:
    tenant, alice = member
    response = upload(api, tenant.ctx.tenant_id, tenant.workspace_id, alice, content=PDF + marker)
    assert (response.status_code, response.json()) == (422, {"detail": "encrypted_pdf"})
    assert fake_clamd.streams == []
    assert _counts(owner_engine, tenant.ctx.tenant_id) == (0, 0, 0, 0)


@pytest.mark.parametrize("address", [None, "", "clamd:3310", "tcp://clamd"])
def test_without_a_usable_scanner_uploads_are_503(
    runtime_settings: DatabaseSettings,
    issuer: Any,  # noqa: F811
    owner_engine: Engine,
    seed_tenant: Seed,
    caplog: pytest.LogCaptureFixture,
    address: str | None,
) -> None:
    # There is no way to skip scanning: without a scanner nothing is uploaded.
    tenant = seed_tenant()
    subject = _subject()
    env = _environ(runtime_settings)
    if address is None:
        del env["CLAMD_ADDRESS"]
    else:
        env["CLAMD_ADDRESS"] = address
    app = create_app(env, jwks_fetcher=_fetcher(issuer))
    with caplog.at_level(logging.WARNING), TestClient(app) as client:
        api = Api(client, issuer, owner_engine)
        api.grant(subject, tenant.ctx.tenant_id)
        response = upload(api, tenant.ctx.tenant_id, tenant.workspace_id, subject)
    assert (response.status_code, response.json()) == (503, {"detail": "scan_unavailable"})
    assert response.headers["cache-control"] == "no-store"
    assert "malware scanner is not configured" in caplog.text
    assert "upload refused: scan_unavailable (not_configured)" in caplog.text
    assert _counts(owner_engine, tenant.ctx.tenant_id) == (0, 0, 0, 0)


def _version_of(owner_engine: Engine, document_id: str) -> UUID:
    with owner_engine.connect() as connection:
        version: UUID = connection.execute(
            text("SELECT id FROM document_versions WHERE document_id = :d"), {"d": document_id}
        ).scalar_one()
    return version


def test_a_blocked_version_is_listed_but_not_downloadable(
    api: Api,  # noqa: F811
    owner_engine: Engine,
    member: tuple[SeededTenant, str],
) -> None:
    # A later non-clean verdict (a future rescan, appended here as the owner) wins.
    tenant, alice = member
    created = upload(api, tenant.ctx.tenant_id, tenant.workspace_id, alice).json()
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO document_scans (tenant_id, version_id, scan_number, verdict, engine, "
                "engine_version, signature_version, signature_date, created_by) "
                "SELECT tenant_id, version_id, 2, 'infected', engine, engine_version, "
                "signature_version, signature_date, created_by FROM document_scans "
                "WHERE version_id = :v"
            ),
            {"v": _version_of(owner_engine, created["id"])},
        )
    docs = _docs(tenant.ctx.tenant_id, tenant.workspace_id)
    listed = api.request("GET", docs, alice).json()["documents"]
    assert [d["version"]["scan_status"] for d in listed] == ["blocked"]
    meta = api.request("GET", f"{docs}/{created['id']}", alice).json()
    assert meta["version"]["scan_status"] == "blocked"
    content = api.request("GET", f"{docs}/{created['id']}/content", alice)
    assert (content.status_code, content.json()) == (409, {"detail": "blocked"})


def test_a_version_uploaded_before_scanning_is_listed_but_not_downloadable(
    api: Api,  # noqa: F811
    owner_engine: Engine,
    member: tuple[SeededTenant, str],
) -> None:
    # Pre-0007 documents have no scan (simulated by suspending the trigger).
    tenant, alice = member
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE document_versions DISABLE TRIGGER document_versions_require_clean_scan"
            )
        )
        user_id: UUID = connection.execute(
            text("SELECT id FROM users WHERE subject = :s"), {"s": alice}
        ).scalar_one()
        document_id: UUID = connection.execute(
            text(
                "INSERT INTO documents (tenant_id, workspace_id, display_name, created_by) "
                "VALUES (:t, :w, 'legacy.pdf', :u) RETURNING id"
            ),
            {"t": tenant.ctx.tenant_id, "w": tenant.workspace_id, "u": user_id},
        ).scalar_one()
        version_id: UUID = connection.execute(
            text(
                "INSERT INTO document_versions (tenant_id, document_id, version_number, "
                "media_type, size_bytes, sha256, created_by) VALUES "
                "(:t, :d, 1, 'application/pdf', :n, :h, :u) RETURNING id"
            ),
            {
                "t": tenant.ctx.tenant_id,
                "d": document_id,
                "n": len(PDF),
                "h": hashlib.sha256(PDF).digest(),
                "u": user_id,
            },
        ).scalar_one()
        connection.execute(
            text("INSERT INTO document_contents VALUES (:v, :t, :c)"),
            {"v": version_id, "t": tenant.ctx.tenant_id, "c": PDF},
        )
        connection.execute(
            text(
                "ALTER TABLE document_versions ENABLE TRIGGER document_versions_require_clean_scan"
            )
        )
    docs = _docs(tenant.ctx.tenant_id, tenant.workspace_id)
    listed = api.request("GET", docs, alice).json()["documents"]
    assert [(d["name"], d["version"]["scan_status"]) for d in listed] == [
        ("legacy.pdf", "not_scanned")
    ]
    content = api.request("GET", f"{docs}/{document_id}/content", alice)
    assert (content.status_code, content.json()) == (409, {"detail": "not_scanned"})


# --- authentication, availability, logs ---------------------------------------------------------


def test_every_document_endpoint_requires_a_token(
    api: Api,  # noqa: F811
    member: tuple[SeededTenant, str],
) -> None:
    tenant, alice = member
    t, w = tenant.ctx.tenant_id, tenant.workspace_id
    doc = upload(api, t, w, alice).json()["id"]
    for response in (
        api.request("GET", _docs(t, w), None),
        api.request("GET", f"{_docs(t, w)}/{doc}", None),
        api.request("GET", f"{_docs(t, w)}/{doc}/content", None),
        upload(api, t, w, None),
    ):
        assert (response.status_code, response.json()) == (401, {"detail": "unauthorized"})


def test_database_outage_is_503(runtime_settings: DatabaseSettings, issuer: Any) -> None:  # noqa: F811
    env = _environ(
        runtime_settings, DATABASE_URL="postgresql://app_runtime:pw@127.0.0.1:1/app_test"
    )
    app = create_app(env, jwks_fetcher=_fetcher(issuer))
    headers = {"Authorization": f"Bearer {issuer.token(sub=_subject())}"}
    with TestClient(app) as client:
        path = _docs(uuid4(), uuid4())
        responses = [
            client.get(path, headers=headers),
            client.get(f"{path}/{uuid4()}/content", headers=headers),
            client.post(
                path,
                headers={**headers, "Content-Type": "application/pdf", "X-Document-Name": "x.pdf"},
                content=PDF,
            ),
        ]
    assert [(r.status_code, r.json()) for r in responses] == [(503, {"detail": "unavailable"})] * 3


def test_logs_never_contain_names_content_or_hashes(
    api: Api,  # noqa: F811
    member: tuple[SeededTenant, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    tenant, alice = member
    secret_name = f"secret-merger-{uuid4()}.pdf"
    content = b"%PDF-1.7\nconfidential-marker-" + uuid4().hex.encode()
    with caplog.at_level(logging.DEBUG):
        created = upload(
            api, tenant.ctx.tenant_id, tenant.workspace_id, alice, content=content, name=secret_name
        )
        upload(api, tenant.ctx.tenant_id, uuid4(), alice, content=content, name=secret_name)
        api.request(
            "GET",
            f"{_docs(tenant.ctx.tenant_id, tenant.workspace_id)}/{created.json()['id']}/content",
            alice,
        )
    text_logged = caplog.text
    assert secret_name not in text_logged
    assert "confidential-marker" not in text_logged
    assert created.json()["version"]["sha256"] not in text_logged
