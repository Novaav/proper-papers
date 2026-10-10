import logging
import os
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import quote, unquote
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from api.auth import (
    AuthConfigError,
    AuthSettings,
    InvalidToken,
    JwksCache,
    JwksFetcher,
    TokenVerifier,
    VerifiedIdentity,
    https_jwks_fetcher,
)
from api.authorization import authorize_tenant, parse_resource_id
from api.database import (
    DatabaseConfigError,
    DatabaseRoleError,
    DatabaseSettings,
    TenantContext,
    create_runtime_engine,
    create_session_factory,
    tenant_transaction,
)
from api.documents import (
    CleanScan,
    DocumentRecord,
    DocumentTooLarge,
    EncryptedPdf,
    InvalidDocumentName,
    NotAPdf,
    WorkspaceNotFound,
    check_not_encrypted,
    check_pdf_content,
    create_document,
    get_document,
    list_documents,
    normalize_document_name,
    read_document_content,
)
from api.extraction.contract import ExtractionResult, FieldKey
from api.identity import identity_transaction, list_memberships
from api.models import DOCUMENT_MAX_BYTES
from api.processing import (
    RunRecord,
    VersionNotClean,
    latest_processing,
    parser_image_id_from_env,
    pipeline_version,
    start_processing,
)
from api.review import (
    ApprovalRecord,
    DecisionRecord,
    ReviewRefused,
    StoredRecordInvalid,
    approve,
    export_record,
    record_decision,
    review_state,
)
from api.scanning import ClamdScanner, ScannerConfigError, ScannerUnavailable, Verdict
from api.workspaces import (
    InvalidWorkspaceName,
    WorkspaceRecord,
    create_workspace,
    get_workspace,
    list_workspaces,
)

logger = logging.getLogger(__name__)

# Authenticated responses (and their errors) are user-specific: never cache them.
NO_STORE = {"Cache-Control": "no-store"}

# Upper bounds for one readiness check: establishing a connection, waiting for
# a pooled connection, and running the query.
CONNECT_TIMEOUT_SECONDS = 2
POOL_TIMEOUT_SECONDS = 2
READINESS_QUERY_TIMEOUT_MS = 1000

# A create-workspace body is a name of at most 200 characters; anything much
# larger is rejected without being read in full.
MAX_BODY_BYTES = 8192

# Content types a client may declare for an upload. They are hints only: the
# server checks the bytes itself and always stores application/pdf.
DOCUMENT_UPLOAD_TYPES = ("application/pdf", "application/octet-stream")
DOCUMENT_NAME_HEADER = "x-document-name"

# Downloads are always attachments, never rendered inline, and sandboxed in case
# a browser displays them anyway.
DOWNLOAD_HEADERS = {
    **NO_STORE,
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
}

# Raised by row-level security when a write is not allowed.
INSUFFICIENT_PRIVILEGE = "42501"
# A review write whose expected number lost a race (unique or sequence check), or
# an approval whose decisions changed underneath it (migration 0009's RV001).
REVIEW_RACE = ("23505", "RV001")


class LivenessResponse(BaseModel):
    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "unavailable"]


class ScannerHealthResponse(BaseModel):
    status: Literal["ok", "unavailable"]


class MembershipResponse(BaseModel):
    tenant_id: UUID
    role: Literal["owner", "reviewer", "member"]


class MeResponse(BaseModel):
    user_id: UUID
    memberships: list[MembershipResponse]


class ErrorResponse(BaseModel):
    detail: Literal[
        "unauthorized",
        "unavailable",
        "not_found",
        "invalid_name",
        "too_large",
        "unsupported_media_type",
        "encrypted_pdf",
        "malware_detected",
        "scan_incomplete",
        "scan_unavailable",
        "not_scanned",
        "blocked",
        "processing_unavailable",
        "not_reviewer",
        "invalid_request",
        "invalid_decision",
        "stale",
        "run_changed",
    ]


class UnresolvedFieldsResponse(BaseModel):
    detail: Literal["unresolved_fields"]
    fields: list[FieldKey]


class WorkspaceResponse(BaseModel):
    id: UUID
    name: str
    created_at: datetime


class WorkspaceListResponse(BaseModel):
    workspaces: list[WorkspaceResponse]


class DocumentVersionResponse(BaseModel):
    id: UUID
    number: int
    media_type: str
    size_bytes: int
    sha256: str
    # Only "clean" versions can be downloaded (409 otherwise).
    scan_status: Literal["clean", "not_scanned", "blocked"]


class DocumentResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    created_at: datetime
    version: DocumentVersionResponse


class DocumentListResponse(BaseModel):
    documents: list[DocumentResponse]


class ProcessingRunResponse(BaseModel):
    id: UUID
    status: Literal["queued", "running", "completed", "needs_review", "failed"]
    failure_code: str | None
    attempts: int
    pipeline_version: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class StartProcessingResponse(BaseModel):
    run: ProcessingRunResponse


class ProcessingResponse(BaseModel):
    # run is null when processing was never started; result only once the run
    # completed or needs review (fields, candidates and their exact evidence).
    run: ProcessingRunResponse | None
    result: ExtractionResult | None


class ReviewDecisionResponse(BaseModel):
    seq: int
    field_key: FieldKey
    resolution: Literal["candidate", "unknown"]
    candidate_ordinal: int | None
    decided_by: UUID
    decided_at: datetime


class ApprovalResponse(BaseModel):
    version: int
    run_id: UUID
    decisions_through: int
    approved_by: UUID
    approved_at: datetime
    record_sha256: str


class ReviewResponse(BaseModel):
    # run_id is null until the newest run completed or needs review; decisions are
    # that run's, in order; approvals are every approved version of the document.
    run_id: UUID | None
    can_review: bool
    decisions_through: int
    decisions: list[ReviewDecisionResponse]
    approvals: list[ApprovalResponse]


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: UUID
    expected_seq: Annotated[int, Field(ge=0)]
    field_key: FieldKey
    resolution: Literal["candidate", "unknown"]
    # The candidate's position in the processing result; only with "candidate".
    candidate_ordinal: Annotated[int, Field(ge=0)] | None = None


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: UUID
    expected_seq: Annotated[int, Field(ge=0)]
    expected_version: Annotated[int, Field(ge=0)]


class CreateWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: StrictStr


_TENANT_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid access token"},
    404: {
        "model": ErrorResponse,
        "description": "Unknown or malformed ID, or the caller is not a member of the tenant",
    },
    503: {"model": ErrorResponse, "description": "Authentication or database unavailable"},
}


def create_app(
    environ: Mapping[str, str] | None = None,
    *,
    jwks_fetcher: Callable[[str], JwksFetcher] = https_jwks_fetcher,
    scanner: ClamdScanner | None = None,
) -> FastAPI:
    """Build the API. Configuration is read at startup, never at import.

    `environ` defaults to the process environment and `jwks_fetcher` fetches
    the issuer's keys over HTTPS; tests replace both. `scanner` replaces the
    one configured by CLAMD_ADDRESS (tests only).
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        env = os.environ if environ is None else environ
        engine = _create_engine(env)
        app.state.engine = engine
        app.state.sessions = None if engine is None else create_session_factory(engine)
        app.state.token_verifier = _create_token_verifier(env, jwks_fetcher)
        app.state.scanner = scanner if scanner is not None else _create_scanner(env)
        # Unset: processing cannot start (503). Malformed: the API refuses to start.
        app.state.parser_image_id = parser_image_id_from_env(env)
        try:
            yield
        finally:
            app.state.engine = None
            app.state.sessions = None
            app.state.token_verifier = None
            app.state.scanner = None
            app.state.parser_image_id = None
            if engine is not None:
                engine.dispose()

    # The interactive documentation and the OpenAPI schema are off unless
    # API_DOCS=1 (the local make targets set it): they need no authentication,
    # so a deployment should not serve them.
    docs = (os.environ if environ is None else environ).get("API_DOCS", "") == "1"
    app = FastAPI(
        title="Document Intelligence API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )

    @app.get("/health/live")
    def health_live() -> LivenessResponse:
        """Report that the process is running and serving HTTP. Checks no dependencies."""
        return LivenessResponse(status="ok")

    @app.get(
        "/health/ready",
        responses={503: {"model": ReadinessResponse, "description": "Database unavailable"}},
    )
    def health_ready(request: Request, response: Response) -> ReadinessResponse:
        """Report whether the API can reach its database as the runtime role."""
        engine: Engine | None = getattr(request.app.state, "engine", None)
        if engine is None or not _database_answers(engine):
            response.status_code = 503
            return ReadinessResponse(status="unavailable")
        return ReadinessResponse(status="ready")

    @app.get(
        "/health/scanner",
        responses={
            503: {"model": ScannerHealthResponse, "description": "No acceptable scanner"},
        },
    )
    def health_scanner(request: Request, response: Response) -> ScannerHealthResponse:
        """Report whether uploads could be scanned now (internal operational endpoint).

        200 only when clamd answers and its signatures meet the upload rules
        (the same client and freshness check as a scan); 503 otherwise. The
        body never contains versions, addresses or signature dates. Readiness
        (/health/ready) does not depend on the scanner: reads and downloads
        keep working during a scanner outage.
        """
        response.headers.update(NO_STORE)
        scanner: ClamdScanner | None = getattr(request.app.state, "scanner", None)
        try:
            if scanner is None:
                raise ScannerUnavailable("not_configured")
            scanner.check()
        except ScannerUnavailable as error:
            logger.info("scanner health: unavailable (%s)", error.reason)
            response.status_code = 503
            return ScannerHealthResponse(status="unavailable")
        return ScannerHealthResponse(status="ok")

    @app.get(
        "/v1/me",
        responses={
            401: {"model": ErrorResponse, "description": "Missing or invalid access token"},
            503: {"model": ErrorResponse, "description": "Authentication or database unavailable"},
        },
    )
    def me(
        request: Request,
        response: Response,
        identity: Annotated[VerifiedIdentity, Depends(require_identity)],
    ) -> MeResponse:
        """The authenticated user and their tenant memberships (user created on first call)."""
        sessions: sessionmaker[Session] | None = getattr(request.app.state, "sessions", None)
        if sessions is None:
            raise _unavailable()
        try:
            with identity_transaction(sessions, identity) as current:
                memberships = list_memberships(current.session)
                user_id = current.user_id
        except (SQLAlchemyError, DatabaseRoleError) as error:
            logger.warning("/v1/me failed: %s", type(error).__name__)
            raise _unavailable() from None
        response.headers.update(NO_STORE)
        return MeResponse(
            user_id=user_id,
            memberships=[
                MembershipResponse(tenant_id=m.tenant_id, role=m.role) for m in memberships
            ],
        )

    @app.get("/v1/tenants/{tenant_id}/workspaces", responses=_TENANT_ERRORS)
    def list_tenant_workspaces(
        tenant_id: str,
        request: Request,
        response: Response,
        identity: Annotated[VerifiedIdentity, Depends(require_identity)],
    ) -> WorkspaceListResponse:
        """The tenant's workspaces, oldest first. Owners and members only."""
        sessions = _sessions(request)
        ctx = _authorize(sessions, identity, tenant_id)
        with _database_errors("list workspaces"), tenant_transaction(sessions, ctx) as session:
            records = list_workspaces(session)
        response.headers.update(NO_STORE)
        return WorkspaceListResponse(workspaces=[_workspace(record) for record in records])

    @app.post(
        "/v1/tenants/{tenant_id}/workspaces",
        status_code=201,
        responses={
            **_TENANT_ERRORS,
            422: {"model": ErrorResponse, "description": "Missing, malformed or invalid name"},
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": CreateWorkspaceRequest.model_json_schema()}
                },
            }
        },
    )
    def create_tenant_workspace(
        tenant_id: str,
        request: Request,
        response: Response,
        identity: Annotated[VerifiedIdentity, Depends(require_identity)],
        body: Annotated[bytes | None, Depends(_read_body)],
    ) -> WorkspaceResponse:
        """Create a workspace in the tenant. Owners and members only.

        The body is validated only after authentication and authorization, so
        callers who may not use the tenant always get 401 or 404, never 422.
        """
        sessions = _sessions(request)
        ctx = _authorize(sessions, identity, tenant_id)
        if body is None:
            raise _invalid_name()
        try:
            name = CreateWorkspaceRequest.model_validate_json(body).name
        except ValidationError:
            raise _invalid_name() from None
        try:
            with (
                _database_errors("create workspace", rls_rejection_is_not_found=True),
                tenant_transaction(sessions, ctx) as session,
            ):
                record = create_workspace(session, name)
        except InvalidWorkspaceName:
            raise _invalid_name() from None
        response.headers.update(NO_STORE)
        response.headers["Location"] = f"/v1/tenants/{ctx.tenant_id}/workspaces/{record.id}"
        return _workspace(record)

    @app.get("/v1/tenants/{tenant_id}/workspaces/{workspace_id}", responses=_TENANT_ERRORS)
    def get_tenant_workspace(
        tenant_id: str,
        workspace_id: str,
        request: Request,
        response: Response,
        identity: Annotated[VerifiedIdentity, Depends(require_identity)],
    ) -> WorkspaceResponse:
        """One workspace of the tenant. Owners and members only."""
        sessions = _sessions(request)
        ctx = _authorize(sessions, identity, tenant_id)
        parsed = parse_resource_id(workspace_id)
        if parsed is None:
            raise _not_found()
        with _database_errors("get workspace"), tenant_transaction(sessions, ctx) as session:
            record = get_workspace(session, parsed)
        if record is None:
            raise _not_found()
        response.headers.update(NO_STORE)
        return _workspace(record)

    documents_path = "/v1/tenants/{tenant_id}/workspaces/{workspace_id}/documents"

    @app.post(
        documents_path,
        status_code=201,
        responses={
            **_TENANT_ERRORS,
            413: {"model": ErrorResponse, "description": f"Larger than {DOCUMENT_MAX_BYTES} bytes"},
            415: {"model": ErrorResponse, "description": "Not a PDF"},
            422: {
                "model": ErrorResponse,
                "description": "Missing or invalid document name (invalid_name); an encrypted "
                "PDF (encrypted_pdf); the malware scan found a threat (malware_detected) or "
                "could not inspect the whole file (scan_incomplete). Nothing is stored.",
            },
            503: {
                "model": ErrorResponse,
                "description": "Authentication or database unavailable (unavailable), or no "
                "acceptable malware-scan verdict could be obtained (scan_unavailable). "
                "Nothing is stored.",
            },
        },
        openapi_extra={
            "parameters": [
                {
                    "name": "X-Document-Name",
                    "in": "header",
                    "required": True,
                    "description": "Display name, percent-encoded UTF-8 (display only).",
                    "schema": {"type": "string"},
                }
            ],
            "requestBody": {
                "required": True,
                "content": {
                    media_type: {"schema": {"type": "string", "format": "binary"}}
                    for media_type in DOCUMENT_UPLOAD_TYPES
                },
            },
        },
    )
    def upload_document(
        tenant_id: str,
        workspace_id: str,
        request: Request,
        response: Response,
        ctx: Annotated[TenantContext, Depends(_authorize_upload)],
        body: Annotated[bytes | None, Depends(_read_authorized_document_body)],
    ) -> DocumentResponse:
        """Upload a PDF as a new document (version 1) in the workspace.

        The caller's membership and the workspace are checked before the body
        is read (`_authorize_upload`); non-members get 404 without the
        application reading any of it. The declared content type and name are
        hints: the bytes must start with the PDF signature, must not carry an
        encryption marker, and must be scanned clean by the malware scanner
        before anything is stored (every other scan outcome stores nothing).
        The document, its version, its bytes and its clean scan record are
        stored in one transaction, which checks membership and the workspace
        again (row-level security), so a revocation in between stores nothing.
        """
        sessions = _sessions(request)
        workspace = _parse_id(workspace_id)
        if body is None:
            raise _document_too_large()
        declared = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if declared not in DOCUMENT_UPLOAD_TYPES:
            raise _not_a_pdf()
        name = _document_name(request)
        if name is None:
            raise _invalid_name()
        # The same checks create_document repeats, made before the scan so that
        # invalid uploads never reach the scanner.
        try:
            normalize_document_name(name)
            check_pdf_content(body)
            check_not_encrypted(body)
        except InvalidDocumentName:
            raise _invalid_name() from None
        except DocumentTooLarge:
            raise _document_too_large() from None
        except NotAPdf:
            raise _not_a_pdf() from None
        except EncryptedPdf:
            logger.info("upload refused: encrypted_pdf (marker)")
            raise _encrypted_pdf() from None
        scan = _scan(request, body)
        try:
            with (
                _database_errors("upload document", rls_rejection_is_not_found=True),
                tenant_transaction(sessions, ctx) as session,
            ):
                record = create_document(session, workspace, name, body, scan)
        except WorkspaceNotFound:
            raise _not_found() from None
        except InvalidDocumentName:
            raise _invalid_name() from None
        except DocumentTooLarge:
            raise _document_too_large() from None
        except NotAPdf:
            raise _not_a_pdf() from None
        except EncryptedPdf:
            raise _encrypted_pdf() from None
        response.headers.update(NO_STORE)
        response.headers["Location"] = (
            f"/v1/tenants/{ctx.tenant_id}/workspaces/{workspace}/documents/{record.id}"
        )
        return _document(record)

    @app.get(documents_path, responses=_TENANT_ERRORS)
    def list_workspace_documents(
        tenant_id: str,
        workspace_id: str,
        request: Request,
        response: Response,
        identity: Annotated[VerifiedIdentity, Depends(require_identity)],
    ) -> DocumentListResponse:
        """The workspace's documents, newest first. Owners and members only."""
        sessions = _sessions(request)
        ctx = _authorize(sessions, identity, tenant_id)
        workspace = _parse_id(workspace_id)
        try:
            with _database_errors("list documents"), tenant_transaction(sessions, ctx) as session:
                records = list_documents(session, workspace)
        except WorkspaceNotFound:
            raise _not_found() from None
        response.headers.update(NO_STORE)
        return DocumentListResponse(documents=[_document(record) for record in records])

    @app.get(f"{documents_path}/{{document_id}}", responses=_TENANT_ERRORS)
    def get_workspace_document(
        tenant_id: str,
        workspace_id: str,
        document_id: str,
        request: Request,
        response: Response,
        identity: Annotated[VerifiedIdentity, Depends(require_identity)],
    ) -> DocumentResponse:
        """One document's metadata. Owners and members only."""
        sessions = _sessions(request)
        ctx = _authorize(sessions, identity, tenant_id)
        workspace, document = _parse_id(workspace_id), _parse_id(document_id)
        with _database_errors("get document"), tenant_transaction(sessions, ctx) as session:
            record = get_document(session, workspace, document)
        if record is None:
            raise _not_found()
        response.headers.update(NO_STORE)
        return _document(record)

    @app.get(
        f"{documents_path}/{{document_id}}/content",
        response_class=Response,
        responses={
            **_TENANT_ERRORS,
            200: {
                "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}},
                "description": "The stored bytes, as an attachment",
            },
        },
    )
    def download_document(
        tenant_id: str,
        workspace_id: str,
        document_id: str,
        request: Request,
        identity: Annotated[VerifiedIdentity, Depends(require_identity)],
    ) -> Response:
        """The document's original bytes, as an attachment (never inline).

        Authorization (token, membership, row-level security) runs on every call.
        """
        sessions = _sessions(request)
        ctx = _authorize(sessions, identity, tenant_id)
        workspace, document = _parse_id(workspace_id), _parse_id(document_id)
        with _database_errors("download document"), tenant_transaction(sessions, ctx) as session:
            found = read_document_content(session, workspace, document)
        if found is None:
            raise _not_found()
        record, content = found
        if content is None:
            # Listed but not clean (never scanned, or blocked by a later verdict).
            detail = "not_scanned" if record.scan_status == "not_scanned" else "blocked"
            raise HTTPException(409, detail=detail, headers=NO_STORE)
        return Response(
            content=content,
            media_type=record.media_type,
            headers={
                **DOWNLOAD_HEADERS,
                "Content-Disposition": _attachment(record.name),
            },
        )

    processing_path = f"{documents_path}/{{document_id}}/processing"
    not_clean: dict[str, object] = {
        "model": ErrorResponse,
        "description": "The document's latest malware verdict is not clean "
        "(not_scanned or blocked)",
    }

    @app.post(
        processing_path,
        status_code=202,
        responses={
            **_TENANT_ERRORS,
            200: {"model": StartProcessingResponse, "description": "Already started (any status)"},
            409: not_clean,
            503: {
                "model": ErrorResponse,
                "description": "Processing is not configured (processing_unavailable), or "
                "authentication or the database is unavailable",
            },
        },
    )
    def start_document_processing(
        tenant_id: str,
        workspace_id: str,
        document_id: str,
        request: Request,
        response: Response,
        identity: Annotated[VerifiedIdentity, Depends(require_identity)],
    ) -> StartProcessingResponse:
        """Queue the document for processing (202), or return the existing run (200).

        Idempotent per document version and pipeline version (the extractor and
        the exact parser image); a failed run is not queued again. Owners and
        members only; the latest malware verdict must be clean.
        """
        sessions = _sessions(request)
        ctx = _authorize(sessions, identity, tenant_id)
        workspace, document = _parse_id(workspace_id), _parse_id(document_id)
        image: str | None = getattr(request.app.state, "parser_image_id", None)
        if image is None:
            raise HTTPException(503, detail="processing_unavailable", headers=NO_STORE)
        try:
            with (
                _database_errors("start processing", rls_rejection_is_not_found=True),
                tenant_transaction(sessions, ctx) as session,
            ):
                started = start_processing(
                    session,
                    workspace,
                    document,
                    pipeline=pipeline_version(image),
                    user_id=ctx.user_id,
                )
        except VersionNotClean as error:
            raise HTTPException(409, detail=error.scan_status, headers=NO_STORE) from None
        if started is None:
            raise _not_found()
        run, created = started
        response.status_code = 202 if created else 200
        response.headers.update(NO_STORE)
        return StartProcessingResponse(run=_run(run))

    @app.get(processing_path, responses={**_TENANT_ERRORS, 409: not_clean})
    def get_document_processing(
        tenant_id: str,
        workspace_id: str,
        document_id: str,
        request: Request,
        response: Response,
        identity: Annotated[VerifiedIdentity, Depends(require_identity)],
    ) -> ProcessingResponse:
        """The newest processing run of the document and, once finished, its result.

        `run` is null if processing was never started. Owners and members only;
        the result (document text as quotes) only while the latest malware
        verdict is clean.
        """
        sessions = _sessions(request)
        ctx = _authorize(sessions, identity, tenant_id)
        workspace, document = _parse_id(workspace_id), _parse_id(document_id)
        try:
            with (
                _database_errors("read processing"),
                tenant_transaction(sessions, ctx) as session,
            ):
                found = latest_processing(session, workspace, document)
        except VersionNotClean as error:
            raise HTTPException(409, detail=error.scan_status, headers=NO_STORE) from None
        except ValidationError:
            # A stored result that no longer satisfies the contract is never served.
            logger.warning("read processing failed: stored result invalid")
            raise _unavailable() from None
        if found is None:
            raise _not_found()
        run, result = found
        response.headers.update(NO_STORE)
        return ProcessingResponse(run=None if run is None else _run(run), result=result)

    review_path = f"{documents_path}/{{document_id}}/review"
    review_write_errors: dict[int | str, dict[str, object]] = {
        **_TENANT_ERRORS,
        403: {"model": ErrorResponse, "description": "Members cannot review (not_reviewer)"},
        409: {
            "model": ErrorResponse,
            "description": "The record changed since it was read (stale), the run is no longer "
            "the document's reviewable run (run_changed), or the latest verdict is not clean",
        },
    }

    @app.get(review_path, responses={**_TENANT_ERRORS, 409: not_clean})
    def get_document_review(
        tenant_id: str,
        workspace_id: str,
        document_id: str,
        request: Request,
        response: Response,
        identity: Annotated[VerifiedIdentity, Depends(require_identity)],
    ) -> ReviewResponse:
        """The review state: the reviewable run, its decisions, and every approved version.

        Owners, reviewers and members read it; `can_review` says whether the caller may
        write (owners and reviewers).
        """
        sessions = _sessions(request)
        ctx = _authorize(sessions, identity, tenant_id)
        workspace, document = _parse_id(workspace_id), _parse_id(document_id)
        try:
            with _database_errors("read review"), tenant_transaction(sessions, ctx) as session:
                state = review_state(session, workspace, document)
        except VersionNotClean as error:
            raise HTTPException(409, detail=error.scan_status, headers=NO_STORE) from None
        if state is None:
            raise _not_found()
        response.headers.update(NO_STORE)
        return ReviewResponse(
            run_id=state.run_id,
            can_review=ctx.can_review,
            decisions_through=state.decisions_through,
            decisions=[_decision(d) for d in state.decisions],
            approvals=[_approval(a) for a in state.approvals],
        )

    @app.post(
        f"{review_path}/decisions",
        status_code=201,
        responses={
            **review_write_errors,
            422: {
                "model": ErrorResponse,
                "description": "Malformed body (invalid_request), or a decision the field does "
                "not allow (invalid_decision)",
            },
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": DecisionRequest.model_json_schema()}},
            }
        },
    )
    def create_review_decision(
        tenant_id: str,
        workspace_id: str,
        document_id: str,
        request: Request,
        response: Response,
        identity: Annotated[VerifiedIdentity, Depends(require_identity)],
        body: Annotated[bytes | None, Depends(_read_body)],
    ) -> ReviewDecisionResponse:
        """Record a decision on one field: a candidate by ordinal, or UNKNOWN. Reviewers only.

        `expected_seq` is the newest decision number the reviewer saw (0 for none);
        if another decision was recorded since, nothing is written (409 stale).
        """
        sessions = _sessions(request)
        ctx = _authorize(sessions, identity, tenant_id)
        workspace, document = _parse_id(workspace_id), _parse_id(document_id)
        _require_reviewer(ctx)
        parsed = _review_body(DecisionRequest, body)
        try:
            with (
                _database_errors("record decision", rls_rejection_is_not_found=True),
                _review_race(),
                tenant_transaction(sessions, ctx) as session,
            ):
                decision = record_decision(
                    session,
                    workspace,
                    document,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    run_id=parsed.run_id,
                    expected_seq=parsed.expected_seq,
                    field_key=parsed.field_key,
                    resolution=parsed.resolution,
                    candidate_ordinal=parsed.candidate_ordinal,
                )
        except VersionNotClean as error:
            raise HTTPException(409, detail=error.scan_status, headers=NO_STORE) from None
        except ReviewRefused as refusal:
            raise _review_refused(refusal) from None
        except ValidationError:
            logger.warning("record decision failed: stored result invalid")
            raise _unavailable() from None
        if decision is None:
            raise _not_found()
        response.headers.update(NO_STORE)
        return _decision(decision)

    @app.post(
        f"{review_path}/approvals",
        status_code=201,
        response_model=ApprovalResponse,
        responses={
            **review_write_errors,
            422: {
                "model": UnresolvedFieldsResponse,
                "description": "A blocking field has no decision (unresolved_fields, listed), "
                "or the body is malformed (invalid_request)",
            },
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": ApprovalRequest.model_json_schema()}},
            }
        },
    )
    def create_approval(
        tenant_id: str,
        workspace_id: str,
        document_id: str,
        request: Request,
        response: Response,
        identity: Annotated[VerifiedIdentity, Depends(require_identity)],
        body: Annotated[bytes | None, Depends(_read_body)],
    ) -> Response | ApprovalResponse:
        """Approve the record as the next immutable version. Reviewers only.

        Every blocking field (each conflict, each required field not found) needs a
        decision; the other fields are approved as extracted. `expected_seq` and
        `expected_version` are what the reviewer saw; any change since is 409 stale.
        """
        sessions = _sessions(request)
        ctx = _authorize(sessions, identity, tenant_id)
        workspace, document = _parse_id(workspace_id), _parse_id(document_id)
        _require_reviewer(ctx)
        parsed = _review_body(ApprovalRequest, body)
        try:
            with (
                _database_errors("approve record", rls_rejection_is_not_found=True),
                _review_race(),
                tenant_transaction(sessions, ctx) as session,
            ):
                approval = approve(
                    session,
                    workspace,
                    document,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    run_id=parsed.run_id,
                    expected_seq=parsed.expected_seq,
                    expected_version=parsed.expected_version,
                )
        except VersionNotClean as error:
            raise HTTPException(409, detail=error.scan_status, headers=NO_STORE) from None
        except ReviewRefused as refusal:
            if refusal.code == "unresolved_fields":
                return JSONResponse(
                    {"detail": "unresolved_fields", "fields": list(refusal.fields)},
                    status_code=422,
                    headers=NO_STORE,
                )
            raise _review_refused(refusal) from None
        except ValidationError:
            logger.warning("approve record failed: stored result invalid")
            raise _unavailable() from None
        if approval is None:
            raise _not_found()
        response.headers.update(NO_STORE)
        return _approval(approval)

    @app.get(
        f"{review_path}/approvals/{{version}}/export",
        response_class=Response,
        responses={
            **_TENANT_ERRORS,
            200: {
                "content": {"application/json": {"schema": {"type": "string", "format": "binary"}}},
                "description": "The stored approved_record.v1 bytes, as an attachment",
            },
            409: not_clean,
        },
    )
    def export_approved_record(
        tenant_id: str,
        workspace_id: str,
        document_id: str,
        version: str,
        request: Request,
        identity: Annotated[VerifiedIdentity, Depends(require_identity)],
    ) -> Response:
        """One approved version exactly as stored (byte-identical on every call)."""
        sessions = _sessions(request)
        ctx = _authorize(sessions, identity, tenant_id)
        workspace, document = _parse_id(workspace_id), _parse_id(document_id)
        number = _parse_version(version)
        try:
            with _database_errors("export record"), tenant_transaction(sessions, ctx) as session:
                exported = export_record(session, workspace, document, number)
        except VersionNotClean as error:
            raise HTTPException(409, detail=error.scan_status, headers=NO_STORE) from None
        except StoredRecordInvalid as error:
            logger.warning("export record failed: stored record invalid (%s)", error)
            raise _unavailable() from None
        if exported is None:
            raise _not_found()
        stem = exported.name.removesuffix(".pdf").removesuffix(".PDF") or "document"
        return Response(
            content=exported.body,
            media_type="application/json",
            headers={
                **DOWNLOAD_HEADERS,
                "Content-Disposition": _attachment(f"{stem}-approved-v{exported.version}.json"),
                "X-Record-SHA256": exported.sha256.hex(),
            },
        )

    return app


def require_identity(request: Request) -> VerifiedIdentity:
    """The verified identity of the request, or a 401/503 with a fixed body.

    Exactly one `Authorization: Bearer <token>` header is accepted. The token and
    the rejection details are never returned or logged; only a category is logged.
    """
    verifier: TokenVerifier | None = getattr(request.app.state, "token_verifier", None)
    if verifier is None:
        raise _unavailable()
    headers = request.headers.getlist("authorization")
    if not headers:
        raise _unauthorized(invalid_token=False)
    scheme, _, token = headers[0].partition(" ")
    if len(headers) != 1 or scheme.lower() != "bearer" or not token or " " in token:
        logger.info("token rejected: authorization_header")
        raise _unauthorized(invalid_token=True)
    try:
        return verifier.verify(token)
    except InvalidToken as rejection:
        logger.info("token rejected: %s", rejection.reason)
        raise _unauthorized(invalid_token=True) from None


async def _read_limited(request: Request, limit: int) -> bytes | None:
    """The raw request body, or None when it exceeds `limit` bytes.

    A declared Content-Length over the limit is refused without reading; the
    stream is also counted, so a missing or false length cannot bypass it.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


async def _read_body(request: Request) -> bytes | None:
    """A create-workspace body (at most MAX_BODY_BYTES)."""
    return await _read_limited(request, MAX_BODY_BYTES)


async def _read_document_body(request: Request) -> bytes | None:
    """An upload body (at most DOCUMENT_MAX_BYTES)."""
    return await _read_limited(request, DOCUMENT_MAX_BYTES)


def _authorize_upload(
    tenant_id: str,
    workspace_id: str,
    request: Request,
    identity: Annotated[VerifiedIdentity, Depends(require_identity)],
) -> TenantContext:
    """Upload prerequisite, decided before the body is read: the caller is a
    member of the tenant and can see the workspace; otherwise the same 404 as
    for an unknown tenant or workspace."""
    sessions = _sessions(request)
    ctx = _authorize(sessions, identity, tenant_id)
    workspace = _parse_id(workspace_id)
    with _database_errors("authorize upload"), tenant_transaction(sessions, ctx) as session:
        visible = get_workspace(session, workspace) is not None
    if not visible:
        raise _not_found()
    return ctx


async def _read_authorized_document_body(
    request: Request, _ctx: Annotated[TenantContext, Depends(_authorize_upload)]
) -> bytes | None:
    """An upload body, read only once `_authorize_upload` has succeeded.

    The authorization is a dependency of this reader, so the order holds
    regardless of how the endpoint lists its parameters (FastAPI runs it once
    per request and hands the endpoint the same context).
    """
    return await _read_document_body(request)


def _parse_id(value: str) -> UUID:
    parsed = parse_resource_id(value)
    if parsed is None:
        raise _not_found()
    return parsed


def _document_name(request: Request) -> str | None:
    """The percent-encoded UTF-8 name header, decoded; None if missing or malformed."""
    values = request.headers.getlist(DOCUMENT_NAME_HEADER)
    if len(values) != 1:
        return None
    try:
        return unquote(values[0], encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def _attachment(name: str) -> str:
    """Content-Disposition for a download: always an attachment (RFC 6266 / 5987)."""
    fallback = "".join(char if " " <= char <= "~" and char not in '"\\' else "_" for char in name)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def _document(record: DocumentRecord) -> DocumentResponse:
    return DocumentResponse(
        id=record.id,
        workspace_id=record.workspace_id,
        name=record.name,
        created_at=record.created_at,
        version=DocumentVersionResponse(
            id=record.version_id,
            number=record.version_number,
            media_type=record.media_type,
            size_bytes=record.size_bytes,
            sha256=record.sha256.hex(),
            scan_status=record.scan_status,
        ),
    )


def _document_too_large() -> HTTPException:
    return HTTPException(413, detail="too_large", headers=NO_STORE)


def _not_a_pdf() -> HTTPException:
    return HTTPException(415, detail="unsupported_media_type", headers=NO_STORE)


def _encrypted_pdf() -> HTTPException:
    return HTTPException(422, detail="encrypted_pdf", headers=NO_STORE)


def _scan(request: Request, content: bytes) -> CleanScan:
    """A clean verdict for `content`, or the HTTP error for any other outcome.

    Fail closed: no scanner configured, a scanner error, a timeout or stale
    signatures are 503 scan_unavailable; a detection is 422. Only the verdict
    category and the detection name are logged, never names or content.
    """
    scanner: ClamdScanner | None = getattr(request.app.state, "scanner", None)
    if scanner is None:
        logger.warning("upload refused: scan_unavailable (not_configured)")
        raise HTTPException(503, detail="scan_unavailable", headers=NO_STORE)
    try:
        result = scanner.scan(content)
    except ScannerUnavailable as error:
        logger.warning("upload refused: scan_unavailable (%s)", error.reason)
        raise HTTPException(503, detail="scan_unavailable", headers=NO_STORE) from None
    if result.verdict is not Verdict.CLEAN:
        detail = {
            Verdict.INFECTED: "malware_detected",
            Verdict.INCOMPLETE: "scan_incomplete",
            Verdict.ENCRYPTED: "encrypted_pdf",
        }[result.verdict]
        logger.info("upload refused: %s (%s)", detail, result.signature_name)
        raise HTTPException(422, detail=detail, headers=NO_STORE)
    signatures = result.signatures
    return CleanScan(
        engine=signatures.engine,
        engine_version=signatures.engine_version,
        signature_version=signatures.signature_version,
        signature_date=signatures.signature_date,
    )


def _sessions(request: Request) -> sessionmaker[Session]:
    sessions: sessionmaker[Session] | None = getattr(request.app.state, "sessions", None)
    if sessions is None:
        raise _unavailable()
    return sessions


def _authorize(
    sessions: sessionmaker[Session], identity: VerifiedIdentity, tenant_id: str
) -> TenantContext:
    """The caller's context in the path's tenant, or a 404 identical to an unknown tenant."""
    parsed = parse_resource_id(tenant_id)
    if parsed is None:
        raise _not_found()
    with _database_errors("authorize tenant"):
        ctx = authorize_tenant(sessions, identity, parsed)
    if ctx is None:
        raise _not_found()
    return ctx


@contextmanager
def _database_errors(operation: str, *, rls_rejection_is_not_found: bool = False) -> Iterator[None]:
    """Database failures become 503.

    For a write, `rls_rejection_is_not_found` turns a row-level security
    rejection into 404: the membership was revoked after authorization, and the
    caller gets the same answer as any non-member. Reads are never rejected by
    row-level security, so there a privilege error is a misconfiguration (503).
    """
    try:
        yield
    except DBAPIError as error:
        if (
            rls_rejection_is_not_found
            and getattr(error.orig, "sqlstate", None) == INSUFFICIENT_PRIVILEGE
        ):
            logger.info("%s: rejected by row-level security", operation)
            raise _not_found() from None
        logger.warning("%s failed: %s", operation, type(error).__name__)
        raise _unavailable() from None
    except (SQLAlchemyError, DatabaseRoleError) as error:
        logger.warning("%s failed: %s", operation, type(error).__name__)
        raise _unavailable() from None


def _workspace(record: WorkspaceRecord) -> WorkspaceResponse:
    return WorkspaceResponse(id=record.id, name=record.name, created_at=record.created_at)


def _run(run: RunRecord) -> ProcessingRunResponse:
    return ProcessingRunResponse.model_validate(run, from_attributes=True)


def _require_reviewer(ctx: TenantContext) -> None:
    if not ctx.can_review:
        raise HTTPException(403, detail="not_reviewer", headers=NO_STORE)


def _review_body[T: BaseModel](model: type[T], body: bytes | None) -> T:
    if body is None:
        raise HTTPException(422, detail="invalid_request", headers=NO_STORE)
    try:
        return model.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, detail="invalid_request", headers=NO_STORE) from None


@contextmanager
def _review_race() -> Iterator[None]:
    """A review write that lost a race to another write is 409 stale, like a seen change."""
    try:
        yield
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) in REVIEW_RACE:
            logger.info("review write refused: stale (concurrent write)")
            raise HTTPException(409, detail="stale", headers=NO_STORE) from None
        raise


def _review_refused(refusal: ReviewRefused) -> HTTPException:
    status = 422 if refusal.code == "invalid_decision" else 409
    return HTTPException(status, detail=refusal.code, headers=NO_STORE)


def _parse_version(value: str) -> int:
    if not value.isascii() or not value.isdigit() or len(value) > 9 or value.startswith("0"):
        raise _not_found()
    return int(value)


def _decision(record: DecisionRecord) -> ReviewDecisionResponse:
    return ReviewDecisionResponse.model_validate(record, from_attributes=True)


def _approval(record: ApprovalRecord) -> ApprovalResponse:
    return ApprovalResponse(
        version=record.version,
        run_id=record.run_id,
        decisions_through=record.decisions_through,
        approved_by=record.approved_by,
        approved_at=record.approved_at,
        record_sha256=record.record_sha256.hex(),
    )


def _not_found() -> HTTPException:
    return HTTPException(404, detail="not_found", headers=NO_STORE)


def _invalid_name() -> HTTPException:
    return HTTPException(422, detail="invalid_name", headers=NO_STORE)


def _unauthorized(*, invalid_token: bool) -> HTTPException:
    # RFC 6750: no error code when the request carried no credentials.
    challenge = 'Bearer error="invalid_token"' if invalid_token else "Bearer"
    return HTTPException(
        401, detail="unauthorized", headers={"WWW-Authenticate": challenge, **NO_STORE}
    )


def _unavailable() -> HTTPException:
    return HTTPException(503, detail="unavailable", headers=NO_STORE)


def _create_token_verifier(
    environ: Mapping[str, str], jwks_fetcher: Callable[[str], JwksFetcher]
) -> TokenVerifier | None:
    """The token verifier, or None when AUTH_* is missing or invalid (then /v1/me is 503).

    Keys are fetched lazily on the first token, never at startup.
    """
    try:
        settings = AuthSettings.from_env(environ)
    except AuthConfigError as error:
        logger.error("authentication is not configured: %s", error)
        return None
    return TokenVerifier(settings, JwksCache(jwks_fetcher(settings.jwks_url)))


def _create_scanner(environ: Mapping[str, str]) -> ClamdScanner | None:
    """The malware scanner from CLAMD_ADDRESS, or None (then every upload is 503).

    There is no way to skip scanning: without a scanner nothing can be uploaded.
    """
    address = environ.get("CLAMD_ADDRESS", "").strip()
    if not address:
        logger.error("malware scanner is not configured: CLAMD_ADDRESS is not set")
        return None
    try:
        return ClamdScanner(address)
    except ScannerConfigError as error:
        logger.error("malware scanner is not configured: %s", error)
        return None


def _create_engine(environ: Mapping[str, str]) -> Engine | None:
    """The runtime engine, or None when DATABASE_URL is missing or invalid.

    The API still starts without a database so that liveness keeps working;
    readiness reports 503 until the configuration is fixed.
    """
    try:
        settings = DatabaseSettings.from_env(environ)
    except DatabaseConfigError as error:
        logger.error("database is not configured: %s", error)
        return None
    return create_runtime_engine(
        settings,
        pool_timeout=POOL_TIMEOUT_SECONDS,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )


def _database_answers(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text(f"SET LOCAL statement_timeout = {READINESS_QUERY_TIMEOUT_MS}"))
            connection.execute(text("SELECT 1")).scalar_one()
    except (SQLAlchemyError, DatabaseRoleError) as error:
        # The exception text can include host names and SQL; log only its type.
        logger.warning("readiness check failed: %s", type(error).__name__)
        return False
    return True


app = create_app()
