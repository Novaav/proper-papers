"""Document tables at the database level: RLS, grants, keys and checks (app_test, real PG)."""

import hashlib
from collections.abc import Callable, Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from api.database import DatabaseSettings
from api.models import DOCUMENT_MAX_BYTES
from tests.conftest import SeededTenant, insert_membership, insert_user, sqlstate

Seed = Callable[..., SeededTenant]
INSUFFICIENT_PRIVILEGE, FOREIGN_KEY, CHECK, UNIQUE = "42501", "23503", "23514", "23505"
PDF = b"%PDF-1.7\n% synthetic test document\n%%EOF\n"
TABLES = ("documents", "document_versions", "document_contents", "document_scans")


def _expect(connection: Connection, code: str, sql: str, **params: object) -> None:
    with pytest.raises(DBAPIError) as error, connection.begin_nested():
        connection.execute(text(sql), params)
    assert sqlstate(error.value) == code, sql


def _as(connection: Connection, seeded: SeededTenant) -> None:
    connection.execute(
        text("SELECT set_config('app.tenant_id', :t, true), set_config('app.user_id', :u, true)"),
        {"t": str(seeded.ctx.tenant_id), "u": str(seeded.ctx.user_id)},
    )


def _insert_document(connection: Connection, seeded: SeededTenant, content: bytes = PDF) -> UUID:
    """Insert a document + version + content as the transaction's member (runtime path)."""
    document_id: UUID = connection.execute(
        text(
            "INSERT INTO documents (tenant_id, workspace_id, display_name, created_by) "
            "VALUES (:t, :w, 'doc.pdf', :u) RETURNING id"
        ),
        {"t": seeded.ctx.tenant_id, "w": seeded.workspace_id, "u": seeded.ctx.user_id},
    ).scalar_one()
    version_id: UUID = connection.execute(
        text(
            "INSERT INTO document_versions (tenant_id, document_id, version_number, media_type, "
            "size_bytes, sha256, created_by) VALUES (:t, :d, 1, 'application/pdf', :n, :h, :u) "
            "RETURNING id"
        ),
        {
            "t": seeded.ctx.tenant_id,
            "d": document_id,
            "n": len(content),
            "h": hashlib.sha256(content).digest(),
            "u": seeded.ctx.user_id,
        },
    ).scalar_one()
    connection.execute(
        text("INSERT INTO document_contents (version_id, tenant_id, content) VALUES (:v, :t, :c)"),
        {"v": version_id, "t": seeded.ctx.tenant_id, "c": content},
    )
    _insert_scan(connection, seeded, version_id)
    return document_id


def _insert_scan(
    connection: Connection,
    seeded: SeededTenant,
    version_id: UUID,
    number: int = 1,
    verdict: str = "clean",
) -> None:
    connection.execute(
        text(
            "INSERT INTO document_scans (tenant_id, version_id, scan_number, verdict, engine, "
            "engine_version, signature_version, signature_date, created_by) "
            "VALUES (:t, :v, :n, :verdict, 'ClamAV', '1.4.6', '27777', now(), :u)"
        ),
        {
            "t": seeded.ctx.tenant_id,
            "v": version_id,
            "n": number,
            "verdict": verdict,
            "u": seeded.ctx.user_id,
        },
    )


@pytest.fixture
def runtime(runtime_engine: Engine) -> Iterator[Connection]:
    with runtime_engine.connect() as connection:
        connection.begin()
        yield connection
        connection.rollback()


@pytest.fixture
def owner(owner_engine: Engine) -> Iterator[Connection]:
    with owner_engine.connect() as connection:
        connection.begin()
        yield connection
        connection.rollback()


def _counts(connection: Connection) -> tuple[int, ...]:
    return tuple(
        connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() for table in TABLES
    )


def test_member_can_insert_and_read_its_tenants_documents(
    runtime: Connection, seed_tenant: Seed
) -> None:
    a = seed_tenant("member")
    _as(runtime, a)
    document_id = _insert_document(runtime, a)
    stored: bytes = runtime.execute(
        text(
            "SELECT c.content FROM documents d JOIN document_versions v ON v.document_id = d.id "
            "JOIN document_contents c ON c.version_id = v.id WHERE d.id = :d"
        ),
        {"d": document_id},
    ).scalar_one()
    assert bytes(stored) == PDF


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE documents SET display_name = 'x'",
        "UPDATE document_versions SET size_bytes = 1",
        "UPDATE document_contents SET content = '\\x00'",
        "DELETE FROM documents",
        "DELETE FROM document_versions",
        "DELETE FROM document_contents",
        "TRUNCATE document_contents",
        "INSERT INTO documents (id, tenant_id, workspace_id, display_name, created_by) "
        "VALUES (gen_random_uuid(), :t, :w, 'x', :u)",
        "INSERT INTO documents (tenant_id, workspace_id, display_name, created_by, created_at) "
        "VALUES (:t, :w, 'x', :u, now())",
    ],
)
def test_runtime_cannot_change_documents_or_choose_server_columns(
    runtime: Connection, seed_tenant: Seed, sql: str
) -> None:
    # Immutability by privilege: no UPDATE, DELETE or TRUNCATE; ids and times are server-set.
    a = seed_tenant()
    _as(runtime, a)
    _insert_document(runtime, a)
    _expect(
        runtime, INSUFFICIENT_PRIVILEGE, sql, t=a.ctx.tenant_id, w=a.workspace_id, u=a.ctx.user_id
    )


def test_rows_can_only_be_authored_by_the_transactions_user(
    runtime: Connection, owner_engine: Engine, seed_tenant: Seed
) -> None:
    a = seed_tenant()
    with owner_engine.begin() as connection:
        colleague = insert_user(connection)
        insert_membership(connection, a.ctx.tenant_id, colleague, "member")
    _as(runtime, a)
    _expect(
        runtime,
        INSUFFICIENT_PRIVILEGE,
        "INSERT INTO documents (tenant_id, workspace_id, display_name, created_by) "
        "VALUES (:t, :w, 'x', :other)",
        t=a.ctx.tenant_id,
        w=a.workspace_id,
        other=colleague,
    )


def test_runtime_cannot_write_into_another_tenant(runtime: Connection, seed_tenant: Seed) -> None:
    a, b = seed_tenant(), seed_tenant()
    _as(runtime, a)
    # Another tenant's id with its own workspace: rejected by row-level security.
    _expect(
        runtime,
        INSUFFICIENT_PRIVILEGE,
        "INSERT INTO documents (tenant_id, workspace_id, display_name, created_by) "
        "VALUES (:t, :w, 'x', :u)",
        t=b.ctx.tenant_id,
        w=b.workspace_id,
        u=a.ctx.user_id,
    )


def test_a_document_cannot_point_at_another_tenants_workspace(
    owner: Connection, seed_tenant: Seed
) -> None:
    # Composite key (tenant_id, workspace_id): even the owner role cannot link across tenants.
    a, b = seed_tenant(), seed_tenant()
    _expect(
        owner,
        FOREIGN_KEY,
        "INSERT INTO documents (tenant_id, workspace_id, display_name, created_by) "
        "VALUES (:t, :other_workspace, 'x', :u)",
        t=a.ctx.tenant_id,
        other_workspace=b.workspace_id,
        u=a.ctx.user_id,
    )


def test_versions_and_contents_cannot_cross_tenants(
    owner: Connection, runtime: Connection, seed_tenant: Seed
) -> None:
    a, b = seed_tenant(), seed_tenant()
    _as(runtime, a)
    document_a = _insert_document(runtime, a)
    version_a: UUID = runtime.execute(
        text("SELECT id FROM document_versions WHERE document_id = :d"), {"d": document_a}
    ).scalar_one()
    runtime.commit()
    try:
        _expect(
            owner,
            FOREIGN_KEY,
            "INSERT INTO document_versions (tenant_id, document_id, version_number, media_type, "
            "size_bytes, sha256, created_by) VALUES (:t, :d, 2, 'application/pdf', 1, :h, :u)",
            t=b.ctx.tenant_id,
            d=document_a,
            h=b"\x00" * 32,
            u=b.ctx.user_id,
        )
        # A fresh version of a's document (no content yet): content claiming tenant b
        # must still be rejected by the (tenant_id, version_id) key.
        version_2: UUID = owner.execute(
            text(
                "INSERT INTO document_versions (tenant_id, document_id, version_number, "
                "media_type, size_bytes, sha256, created_by) "
                "VALUES (:t, :d, 2, 'application/pdf', 1, :h, :u) RETURNING id"
            ),
            {"t": a.ctx.tenant_id, "d": document_a, "h": b"\x00" * 32, "u": a.ctx.user_id},
        ).scalar_one()
        _expect(
            owner,
            FOREIGN_KEY,
            "INSERT INTO document_contents (version_id, tenant_id, content) VALUES (:v, :t, 'x')",
            v=version_2,
            t=b.ctx.tenant_id,
        )
        assert version_a != version_2
    finally:
        # Release this transaction's locks (the new version row) before cleaning up
        # on another connection, or the cleanup would wait for them forever.
        owner.rollback()
        owner.begin()
        _cleanup(owner.engine, document_a)


@pytest.mark.parametrize(
    ("column", "value", "code"),
    [
        ("media_type", "'text/html'", CHECK),
        ("size_bytes", "0", CHECK),
        ("size_bytes", str(DOCUMENT_MAX_BYTES + 1), CHECK),
        ("sha256", "'\\x00'::bytea", CHECK),
        ("version_number", "0", CHECK),
    ],
)
def test_version_checks(
    owner: Connection, runtime: Connection, seed_tenant: Seed, column: str, value: str, code: str
) -> None:
    a = seed_tenant()
    values = {
        "version_number": "2",
        "media_type": "'application/pdf'",
        "size_bytes": "1",
        "sha256": "decode(repeat('00', 32), 'hex')",
    }
    values[column] = value
    _as(runtime, a)
    document_id = _insert_document(runtime, a)
    _expect(
        runtime,
        code,
        "INSERT INTO document_versions (tenant_id, document_id, version_number, media_type, "
        f"size_bytes, sha256, created_by) VALUES (:t, :d, {values['version_number']}, "
        f"{values['media_type']}, {values['size_bytes']}, {values['sha256']}, :u)",
        t=a.ctx.tenant_id,
        d=document_id,
        u=a.ctx.user_id,
    )


def test_one_version_number_per_document_and_bounded_content(
    runtime: Connection, seed_tenant: Seed
) -> None:
    a = seed_tenant()
    _as(runtime, a)
    document_id = _insert_document(runtime, a)
    _expect(
        runtime,
        UNIQUE,
        "INSERT INTO document_versions (tenant_id, document_id, version_number, media_type, "
        "size_bytes, sha256, created_by) VALUES (:t, :d, 1, 'application/pdf', 1, :h, :u)",
        t=a.ctx.tenant_id,
        d=document_id,
        h=b"\x00" * 32,
        u=a.ctx.user_id,
    )
    _expect(
        runtime,
        CHECK,
        "INSERT INTO documents (tenant_id, workspace_id, display_name, created_by) "
        "VALUES (:t, :w, :name, :u)",
        t=a.ctx.tenant_id,
        w=a.workspace_id,
        name="x" * 256,
        u=a.ctx.user_id,
    )


def test_non_members_and_other_tenants_see_nothing(
    runtime_engine: Engine, owner_engine: Engine, seed_tenant: Seed
) -> None:
    a, b = seed_tenant(), seed_tenant()
    with runtime_engine.begin() as connection:
        _as(connection, a)
        _insert_document(connection, a)
    try:
        with runtime_engine.connect() as connection:
            connection.begin()
            _as(connection, b)
            assert _counts(connection) == (0, 0, 0, 0)
            # b's user with a's tenant setting (not a member): nothing either.
            connection.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(a.ctx.tenant_id)}
            )
            assert _counts(connection) == (0, 0, 0, 0)
            connection.rollback()
            connection.begin()
            _as(connection, a)
            assert _counts(connection) == (1, 1, 1, 1)
            connection.rollback()
        # Revoking the membership hides everything on the next statement.
        with owner_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM memberships WHERE user_id = :u"), {"u": a.ctx.user_id}
            )
        with runtime_engine.connect() as connection:
            connection.begin()
            _as(connection, a)
            assert _counts(connection) == (0, 0, 0, 0)
            connection.rollback()
    finally:
        with owner_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM document_contents WHERE tenant_id = :t"), {"t": a.ctx.tenant_id}
            )
            connection.execute(
                text("DELETE FROM document_scans WHERE tenant_id = :t"), {"t": a.ctx.tenant_id}
            )
            connection.execute(
                text("DELETE FROM document_versions WHERE tenant_id = :t"), {"t": a.ctx.tenant_id}
            )
            connection.execute(
                text("DELETE FROM documents WHERE tenant_id = :t"), {"t": a.ctx.tenant_id}
            )


@pytest.mark.parametrize("table", TABLES)
def test_operator_has_no_access_to_documents(
    operator_settings: DatabaseSettings, table: str
) -> None:
    engine = create_engine(operator_settings.url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            connection.begin()
            _expect(connection, INSUFFICIENT_PRIVILEGE, f"SELECT count(*) FROM {table}")
            connection.rollback()
    finally:
        engine.dispose()


def _cleanup(engine: Engine, document_id: UUID) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM document_scans WHERE version_id IN "
                "(SELECT id FROM document_versions WHERE document_id = :d)"
            ),
            {"d": document_id},
        )
        connection.execute(
            text(
                "DELETE FROM document_contents WHERE version_id IN "
                "(SELECT id FROM document_versions WHERE document_id = :d)"
            ),
            {"d": document_id},
        )
        connection.execute(
            text("DELETE FROM document_versions WHERE document_id = :d"), {"d": document_id}
        )
        connection.execute(text("DELETE FROM documents WHERE id = :d"), {"d": document_id})


def test_unknown_uuid_is_rejected_by_keys(owner: Connection, seed_tenant: Seed) -> None:
    a = seed_tenant()
    _expect(
        owner,
        FOREIGN_KEY,
        "INSERT INTO documents (tenant_id, workspace_id, display_name, created_by) "
        "VALUES (:t, :w, 'x', :missing_user)",
        t=a.ctx.tenant_id,
        w=a.workspace_id,
        missing_user=uuid4(),
    )


# --- malware scans (migration 0007): invariants A and B -----------------------------------------


def _insert_unscanned(connection: Connection, seeded: SeededTenant) -> tuple[UUID, UUID]:
    """Document, version and content without any scan row; returns (document, version)."""
    document_id: UUID = connection.execute(
        text(
            "INSERT INTO documents (tenant_id, workspace_id, display_name, created_by) "
            "VALUES (:t, :w, 'doc.pdf', :u) RETURNING id"
        ),
        {"t": seeded.ctx.tenant_id, "w": seeded.workspace_id, "u": seeded.ctx.user_id},
    ).scalar_one()
    version_id: UUID = connection.execute(
        text(
            "INSERT INTO document_versions (tenant_id, document_id, version_number, media_type, "
            "size_bytes, sha256, created_by) VALUES (:t, :d, 1, 'application/pdf', :n, :h, :u) "
            "RETURNING id"
        ),
        {
            "t": seeded.ctx.tenant_id,
            "d": document_id,
            "n": len(PDF),
            "h": hashlib.sha256(PDF).digest(),
            "u": seeded.ctx.user_id,
        },
    ).scalar_one()
    connection.execute(
        text("INSERT INTO document_contents (version_id, tenant_id, content) VALUES (:v, :t, :c)"),
        {"v": version_id, "t": seeded.ctx.tenant_id, "c": PDF},
    )
    return document_id, version_id


def _visible(connection: Connection, seeded: SeededTenant) -> tuple[int, int]:
    """(versions, contents) the runtime sees as the tenant's member."""
    _as(connection, seeded)
    versions: int = connection.execute(text("SELECT count(*) FROM document_versions")).scalar_one()
    contents: int = connection.execute(text("SELECT count(*) FROM document_contents")).scalar_one()
    return versions, contents


@pytest.mark.parametrize("role", ["runtime", "owner"])
def test_a_version_without_a_clean_first_scan_cannot_commit(
    runtime_engine: Engine, owner_engine: Engine, seed_tenant: Seed, role: str
) -> None:
    # Invariant A holds for every role, including the schema owner.
    a = seed_tenant()
    engine = runtime_engine if role == "runtime" else owner_engine
    with pytest.raises(DBAPIError) as error, engine.begin() as connection:
        _as(connection, a)
        _insert_unscanned(connection, a)
    assert sqlstate(error.value) == CHECK
    with owner_engine.connect() as connection:
        stored: int = connection.execute(
            text("SELECT count(*) FROM documents WHERE tenant_id = :t"), {"t": a.ctx.tenant_id}
        ).scalar_one()
    assert stored == 0


@pytest.mark.parametrize("verdict", ["infected", "incomplete", "encrypted", "error"])
def test_a_non_clean_first_scan_cannot_commit(
    owner_engine: Engine, seed_tenant: Seed, verdict: str
) -> None:
    a = seed_tenant()
    with pytest.raises(DBAPIError) as error, owner_engine.begin() as connection:
        _, version_id = _insert_unscanned(connection, a)
        _insert_scan(connection, a, version_id, verdict=verdict)
    assert sqlstate(error.value) == CHECK


def test_bytes_are_readable_only_while_the_latest_verdict_is_clean(
    runtime_engine: Engine, owner_engine: Engine, seed_tenant: Seed
) -> None:
    # Invariant B, enforced by row-level security whatever the application does.
    a = seed_tenant()
    with runtime_engine.begin() as connection:
        _as(connection, a)
        document_id = _insert_document(connection, a)
    try:
        with owner_engine.connect() as connection:
            version_id: UUID = connection.execute(
                text("SELECT id FROM document_versions WHERE document_id = :d"), {"d": document_id}
            ).scalar_one()
        steps = [(None, (1, 1)), ((2, "infected"), (1, 0)), ((3, "clean"), (1, 1))]
        for scan, expected in steps:
            if scan is not None:
                with owner_engine.begin() as connection:
                    _insert_scan(connection, a, version_id, number=scan[0], verdict=scan[1])
            with runtime_engine.connect() as connection:
                connection.begin()
                assert _visible(connection, a) == expected, scan
                connection.rollback()
    finally:
        _cleanup(owner_engine, document_id)


def test_a_version_uploaded_before_scanning_is_listed_but_not_readable(
    runtime_engine: Engine, owner_engine: Engine, seed_tenant: Seed
) -> None:
    # Pre-0007 rows have no scan; simulated by suspending the trigger (owner only).
    a = seed_tenant()
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE document_versions DISABLE TRIGGER document_versions_require_clean_scan"
            )
        )
        document_id, _ = _insert_unscanned(connection, a)
        connection.execute(
            text(
                "ALTER TABLE document_versions ENABLE TRIGGER document_versions_require_clean_scan"
            )
        )
    try:
        with runtime_engine.connect() as connection:
            connection.begin()
            assert _visible(connection, a) == (1, 0)
            connection.rollback()
    finally:
        _cleanup(owner_engine, document_id)


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        # Only the upload's own scan: number 1, clean, by the transaction's user.
        ("INSERT INTO document_scans (tenant_id, version_id, scan_number, verdict, engine, "
         "engine_version, signature_version, signature_date, created_by) "
         "VALUES (:t, :v, 2, 'clean', 'ClamAV', '1', '1', now(), :u)", INSUFFICIENT_PRIVILEGE),
        ("INSERT INTO document_scans (tenant_id, version_id, scan_number, verdict, engine, "
         "engine_version, signature_version, signature_date, created_by) "
         "VALUES (:t, :v, 1, 'infected', 'ClamAV', '1', '1', now(), :u)", INSUFFICIENT_PRIVILEGE),
        ("INSERT INTO document_scans (tenant_id, version_id, scan_number, verdict, engine, "
         "engine_version, signature_version, signature_date, created_by, scanned_at) "
         "VALUES (:t, :v, 1, 'clean', 'ClamAV', '1', '1', now(), :u, now())",
         INSUFFICIENT_PRIVILEGE),
        ("UPDATE document_scans SET verdict = 'clean'", INSUFFICIENT_PRIVILEGE),
        ("DELETE FROM document_scans", INSUFFICIENT_PRIVILEGE),
        ("TRUNCATE document_scans", INSUFFICIENT_PRIVILEGE),
    ],
)  # fmt: skip
def test_runtime_can_only_append_the_uploads_clean_scan(
    runtime: Connection, seed_tenant: Seed, sql: str, code: str
) -> None:
    a = seed_tenant()
    _as(runtime, a)
    _, version_id = _insert_unscanned(runtime, a)
    _expect(runtime, code, sql, t=a.ctx.tenant_id, v=version_id, u=a.ctx.user_id)


def test_runtime_cannot_record_a_scan_as_another_user_or_tenant(
    runtime: Connection, seed_tenant: Seed
) -> None:
    a, b = seed_tenant(), seed_tenant()
    _as(runtime, a)
    _, version_id = _insert_unscanned(runtime, a)
    insert = (
        "INSERT INTO document_scans (tenant_id, version_id, scan_number, verdict, engine, "
        "engine_version, signature_version, signature_date, created_by) "
        "VALUES (:t, :v, 1, 'clean', 'ClamAV', '1', '1', now(), :u)"
    )
    _expect(
        runtime, INSUFFICIENT_PRIVILEGE, insert, t=a.ctx.tenant_id, v=version_id, u=b.ctx.user_id
    )
    _expect(
        runtime, INSUFFICIENT_PRIVILEGE, insert, t=b.ctx.tenant_id, v=version_id, u=a.ctx.user_id
    )


@pytest.mark.parametrize(
    ("values", "code"),
    [
        ("1, 'clean', '', '1', '1'", CHECK),
        ("1, 'clean', 'ClamAV', '', '1'", CHECK),
        ("1, 'clean', 'ClamAV', '1', ''", CHECK),
        ("1, 'clean', 'ClamAV', '1', repeat('x', 101)", CHECK),
        ("0, 'clean', 'ClamAV', '1', '1'", CHECK),
        ("2, 'maybe', 'ClamAV', '1', '1'", CHECK),
        ("1, 'clean', 'ClamAV', '1', '1'", UNIQUE),  # scan 1 exists: one total order
    ],
)
def test_scan_checks_and_one_verdict_per_scan_number(
    owner: Connection, seed_tenant: Seed, values: str, code: str
) -> None:
    a = seed_tenant()
    _, version_id = _insert_unscanned(owner, a)
    _insert_scan(owner, a, version_id)
    _expect(
        owner,
        code,
        "INSERT INTO document_scans (tenant_id, version_id, scan_number, verdict, engine, "
        f"engine_version, signature_version, signature_date, created_by) "
        f"VALUES (:t, :v, {values}, now(), :u)",
        t=a.ctx.tenant_id,
        v=version_id,
        u=a.ctx.user_id,
    )


def test_the_trigger_function_runs_as_the_invoker_with_a_fixed_search_path(
    owner: Connection,
) -> None:
    security_definer, config = owner.execute(
        text(
            "SELECT prosecdef, proconfig FROM pg_proc "
            "WHERE proname = 'document_versions_require_clean_scan'"
        )
    ).one()
    assert security_definer is False
    assert config == ["search_path=app, pg_temp"]
