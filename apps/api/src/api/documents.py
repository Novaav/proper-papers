"""Document repository, scoped to the tenant and user of the current transaction.

Every function takes the Session yielded by `tenant_transaction` and runs inside
its transaction; nothing here commits. Tenant and author come from the
transaction-local settings, and row-level security limits every statement to
tenants the user is a member of.

Uploaded bytes are untrusted: they are checked (size, PDF signature, no
encryption marker), malware-scanned by the caller, hashed, stored and returned,
but never parsed, rendered or interpreted. A document, its version row, its
bytes and its clean scan record are inserted in the caller's single
transaction, so a failure leaves no partial record. The bytes of a version are
readable only while its latest scan verdict is clean (row-level security).
"""

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Uuid, func, insert, select
from sqlalchemy.orm import Session

from api.models import (
    DOCUMENT_MAX_BYTES,
    DOCUMENT_NAME_MAX_LENGTH,
    Document,
    DocumentContent,
    DocumentScan,
    DocumentVersion,
    Workspace,
)

ScanStatus = Literal["clean", "not_scanned", "blocked"]

PDF_MEDIA_TYPE = "application/pdf"
PDF_SIGNATURE = b"%PDF-"

_CURRENT_TENANT = func.nullif(func.current_setting("app.tenant_id", True), "").cast(Uuid())
_CURRENT_USER = func.nullif(func.current_setting("app.user_id", True), "").cast(Uuid())

# Unicode "format" controls that can disguise a name (bidirectional overrides and
# isolates, zero-width characters); control characters are rejected separately.
_DISGUISING = {
    "​", "‌", "‍", "‎", "‏",
    "‪", "‫", "‬", "‭", "‮",
    "⁦", "⁧", "⁨", "⁩", "﻿",
}  # fmt: skip


# The name /Encrypt as a PDF name token, each character optionally written as
# a #xx hex escape (PDF names allow that), followed by whitespace, a delimiter
# or the end of the data. A supplementary signal only (see check_not_encrypted).
_PDF_DELIMITERS = rb"\x00\t\n\x0c\r ()<>\[\]{}/%"


def _name_token(name: str) -> re.Pattern[bytes]:
    parts = []
    for char in name:
        code = f"{ord(char):02x}"
        hex_digits = b"".join(
            b"[" + digit.encode() + digit.upper().encode() + b"]"
            if digit.isalpha()
            else digit.encode()
            for digit in code
        )
        parts.append(b"(?:" + re.escape(char.encode()) + b"|#" + hex_digits + b")")
    return re.compile(b"/" + b"".join(parts) + b"(?=[" + _PDF_DELIMITERS + b"]|\\Z)")


_ENCRYPT_NAME = _name_token("Encrypt")

# The latest scan verdict of a version (highest scan_number), or NULL if never scanned.
_LATEST_VERDICT = (
    select(DocumentScan.verdict)
    .where(
        DocumentScan.tenant_id == DocumentVersion.tenant_id,
        DocumentScan.version_id == DocumentVersion.id,
    )
    .order_by(DocumentScan.scan_number.desc())
    .limit(1)
    .correlate(DocumentVersion)
    .scalar_subquery()
)

# Document metadata joined with its version 1 (the only version in B1).
_METADATA_COLUMNS = (
    Document.id,
    Document.workspace_id,
    Document.display_name,
    Document.created_at,
    Document.created_by,
    DocumentVersion.id,
    DocumentVersion.version_number,
    DocumentVersion.media_type,
    DocumentVersion.size_bytes,
    DocumentVersion.sha256,
    _LATEST_VERDICT,
)
_VERSION_1 = (
    (DocumentVersion.tenant_id == Document.tenant_id)
    & (DocumentVersion.document_id == Document.id)
    & (DocumentVersion.version_number == 1)
)


class TenantContextMissing(Exception):
    """A repository function ran outside tenant_transaction()."""


class WorkspaceNotFound(Exception):
    """The workspace does not exist in the current tenant (or is not visible)."""


class InvalidDocumentName(ValueError):
    """The name is empty, too long, or contains control or disguising characters."""


class DocumentTooLarge(ValueError):
    """The content exceeds DOCUMENT_MAX_BYTES."""


class NotAPdf(ValueError):
    """The content is empty or does not start with the PDF signature."""


class EncryptedPdf(ValueError):
    """The content carries an /Encrypt name token (an encrypted PDF)."""


@dataclass(frozen=True, slots=True)
class CleanScan:
    """The clean verdict an upload was stored with, and the engine that reached it."""

    engine: str
    engine_version: str
    signature_version: str
    signature_date: datetime


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    id: UUID
    workspace_id: UUID
    name: str
    created_at: datetime
    created_by: UUID
    version_id: UUID
    version_number: int
    media_type: str
    size_bytes: int
    sha256: bytes
    # "clean", "not_scanned" (no verdict, e.g. uploaded before scanning) or
    # "blocked" (the latest verdict is not clean). Only clean bytes are readable.
    scan_status: ScanStatus


def normalize_document_name(name: str) -> str:
    """The display name to store: NFC, without directory parts or surrounding spaces.

    Names are display text only, never paths or storage keys; this keeps them
    readable and unambiguous. Raises InvalidDocumentName.
    """
    if not isinstance(name, str):
        raise InvalidDocumentName("name must be a string")
    normalized = unicodedata.normalize("NFC", name)
    # Keep only the last path segment a browser or client might have sent.
    normalized = normalized.replace("\\", "/").rsplit("/", 1)[-1].strip(" ")
    if not normalized or normalized in {".", ".."}:
        raise InvalidDocumentName("name is empty")
    if len(normalized) > DOCUMENT_NAME_MAX_LENGTH:
        raise InvalidDocumentName(f"name is longer than {DOCUMENT_NAME_MAX_LENGTH} characters")
    for char in normalized:
        if unicodedata.category(char) == "Cc" or char in _DISGUISING:
            raise InvalidDocumentName("name contains control or disguising characters")
    return normalized


def check_pdf_content(content: bytes) -> None:
    """Accept only 1..DOCUMENT_MAX_BYTES bytes starting with the PDF signature.

    The signature proves the format family only; the bytes stay untrusted.
    """
    if not isinstance(content, bytes):
        raise TypeError("content must be bytes")
    if len(content) > DOCUMENT_MAX_BYTES:
        raise DocumentTooLarge(f"content exceeds {DOCUMENT_MAX_BYTES} bytes")
    if not content.startswith(PDF_SIGNATURE):
        raise NotAPdf("content is not a PDF")


def check_not_encrypted(content: bytes) -> None:
    """Reject content that carries the /Encrypt name token (raises EncryptedPdf).

    Covers the document's own encryption when it is declared as the PDF
    specification requires: the /Encrypt key sits in a trailer or
    cross-reference stream dictionary, which is never compressed (hex-escaped
    spellings such as /Encr#79pt included). A parser-free byte pattern, not a
    general encryption detector: it does not see encryption inside embedded
    payloads (compressed attachments) or in malformed files a reader repairs.
    A match elsewhere (uncompressed text naming /Encrypt) is rejected too, on
    the safe side.
    """
    if not isinstance(content, bytes):
        raise TypeError("content must be bytes")
    if _ENCRYPT_NAME.search(content):
        raise EncryptedPdf("content is an encrypted PDF")


def create_document(
    session: Session, workspace_id: UUID, name: str, content: bytes, scan: CleanScan
) -> DocumentRecord:
    """Store a new document (version 1) and its clean scan in a workspace of the current tenant.

    The caller has scanned `content` and obtained a clean verdict; the database
    refuses to commit a version without it (migration 0007).
    """
    if not isinstance(workspace_id, UUID):
        raise TypeError("workspace_id must be a uuid.UUID")
    if not isinstance(scan, CleanScan):
        raise TypeError("scan must be a CleanScan")
    display_name = normalize_document_name(name)
    check_pdf_content(content)
    check_not_encrypted(content)
    _require_context(session)
    _require_workspace(session, workspace_id)
    digest = hashlib.sha256(content).digest()

    document_id, created_at, created_by = session.execute(
        insert(Document)
        .values(
            tenant_id=_CURRENT_TENANT,
            workspace_id=workspace_id,
            display_name=display_name,
            created_by=_CURRENT_USER,
        )
        .returning(Document.id, Document.created_at, Document.created_by)
    ).one()
    version_id = session.execute(
        insert(DocumentVersion)
        .values(
            tenant_id=_CURRENT_TENANT,
            document_id=document_id,
            version_number=1,
            media_type=PDF_MEDIA_TYPE,
            size_bytes=len(content),
            sha256=digest,
            created_by=_CURRENT_USER,
        )
        .returning(DocumentVersion.id)
    ).scalar_one()
    session.execute(
        insert(DocumentContent).values(
            version_id=version_id, tenant_id=_CURRENT_TENANT, content=content
        )
    )
    session.execute(
        insert(DocumentScan).values(
            tenant_id=_CURRENT_TENANT,
            version_id=version_id,
            scan_number=1,
            verdict="clean",
            engine=scan.engine,
            engine_version=scan.engine_version,
            signature_version=scan.signature_version,
            signature_date=scan.signature_date,
            created_by=_CURRENT_USER,
        )
    )
    return DocumentRecord(
        id=document_id,
        workspace_id=workspace_id,
        name=display_name,
        created_at=created_at,
        created_by=created_by,
        version_id=version_id,
        version_number=1,
        media_type=PDF_MEDIA_TYPE,
        size_bytes=len(content),
        sha256=digest,
        scan_status="clean",
    )


def list_documents(session: Session, workspace_id: UUID) -> list[DocumentRecord]:
    """The workspace's documents (latest version each), newest first (ties by id)."""
    if not isinstance(workspace_id, UUID):
        raise TypeError("workspace_id must be a uuid.UUID")
    _require_context(session)
    _require_workspace(session, workspace_id)
    rows = session.execute(
        select(*_METADATA_COLUMNS)
        .join(DocumentVersion, _VERSION_1)
        .where(Document.workspace_id == workspace_id)
        .order_by(Document.created_at.desc(), Document.id.desc())
    )
    return [_record(row) for row in rows]


def get_document(session: Session, workspace_id: UUID, document_id: UUID) -> DocumentRecord | None:
    """The document if it is in this workspace and visible; None otherwise (indistinguishable)."""
    _check_ids(workspace_id, document_id)
    _require_context(session)
    row = session.execute(
        select(*_METADATA_COLUMNS)
        .join(DocumentVersion, _VERSION_1)
        .where(Document.workspace_id == workspace_id, Document.id == document_id)
    ).one_or_none()
    return None if row is None else _record(row)


def read_document_content(
    session: Session, workspace_id: UUID, document_id: UUID
) -> tuple[DocumentRecord, bytes | None] | None:
    """The document's metadata and bytes; None if missing, in another workspace or not visible.

    The bytes are None when the version is not clean. Row-level security
    enforces that too: the runtime cannot read bytes whose latest verdict is
    not clean, even if this check were skipped.
    """
    record = get_document(session, workspace_id, document_id)
    if record is None:
        return None
    if record.scan_status != "clean":
        return record, None
    content = session.execute(
        select(DocumentContent.content).where(DocumentContent.version_id == record.version_id)
    ).scalar_one_or_none()
    return record, None if content is None else bytes(content)


def _record(row: Sequence[Any]) -> DocumentRecord:
    """A DocumentRecord from a row of _METADATA_COLUMNS (same order)."""
    return DocumentRecord(
        id=row[0],
        workspace_id=row[1],
        name=row[2],
        created_at=row[3],
        created_by=row[4],
        version_id=row[5],
        version_number=row[6],
        media_type=row[7],
        size_bytes=row[8],
        sha256=bytes(row[9]),
        scan_status=_scan_status(row[10]),
    )


def _scan_status(latest_verdict: str | None) -> ScanStatus:
    if latest_verdict is None:
        return "not_scanned"
    return "clean" if latest_verdict == "clean" else "blocked"


def _check_ids(*ids: UUID) -> None:
    for value in ids:
        if not isinstance(value, UUID):
            raise TypeError("ids must be uuid.UUID")


def _require_context(session: Session) -> None:
    tenant, user = session.execute(select(_CURRENT_TENANT, _CURRENT_USER)).one()
    if tenant is None or user is None:
        raise TenantContextMissing("repository functions must run inside tenant_transaction()")


def _require_workspace(session: Session, workspace_id: UUID) -> None:
    # Row-level security shows only workspaces of the current tenant that the
    # user is a member of; anything else is "not found".
    found = session.execute(
        select(Workspace.id).where(Workspace.id == workspace_id)
    ).scalar_one_or_none()
    if found is None:
        raise WorkspaceNotFound("workspace not found")
