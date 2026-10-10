from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Engine, MetaData, Text, inspect, text

from api.database import DatabaseSettings
from api.models import Base
from tests.conftest import SeededTenant

PYPROJECT = Path(__file__).parents[1] / "pyproject.toml"
APPLICATION_TABLES = {
    "tenants",
    "workspaces",
    "users",
    "memberships",
    "operator_audit",
    "documents",
    "document_versions",
    "document_contents",
    "document_scans",
    "processing_runs",
    "document_pages",
    "extracted_fields",
    "field_candidates",
    "candidate_evidence",
    "review_decisions",
    "approved_records",
}


@pytest.fixture
def alembic_config(owner_settings: DatabaseSettings) -> Config:
    # Explicit URL of the verified disposable test database; the environment's
    # DATABASE_URL_OWNER (development) is never used by the tests.
    config = Config(toml_file=str(PYPROJECT))
    config.attributes["owner_url"] = owner_settings.url.render_as_string(hide_password=False)
    return config


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def test_upgrade_downgrade_upgrade_cycle(alembic_config: Config, owner_engine: Engine) -> None:
    try:
        command.downgrade(alembic_config, "base")
        assert _tables(owner_engine) == {"alembic_version"}

        command.upgrade(alembic_config, "head")
        assert _tables(owner_engine) == APPLICATION_TABLES | {"alembic_version"}

        command.downgrade(alembic_config, "base")
        assert _tables(owner_engine) == {"alembic_version"}
    finally:
        command.upgrade(alembic_config, "head")

    assert _tables(owner_engine) == APPLICATION_TABLES | {"alembic_version"}


_CATALOG_QUERIES = {
    "policies": "SELECT tablename, policyname, permissive, roles::text, cmd, qual, with_check "
    "FROM pg_policies WHERE schemaname = 'app' ORDER BY 1, 2",
    "table_grants": "SELECT grantee, table_name, privilege_type "
    "FROM information_schema.role_table_grants WHERE table_schema = 'app' "
    "AND grantee IN ('app_runtime', 'app_operator', 'app_worker', 'PUBLIC') ORDER BY 1, 2, 3",
    "column_grants": "SELECT grantee, table_name, column_name, privilege_type "
    "FROM information_schema.column_privileges WHERE table_schema = 'app' "
    "AND grantee IN ('app_runtime', 'app_operator', 'app_worker', 'PUBLIC') "
    "ORDER BY 1, 2, 3, 4",
    "rls": "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
    "WHERE relnamespace = 'app'::regnamespace AND relkind = 'r' ORDER BY 1",
    "triggers": "SELECT tgrelid::regclass::text, tgname, tgdeferrable, tginitdeferred "
    "FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1, 2",
    "functions": "SELECT proname, prosecdef, proconfig::text FROM pg_proc "
    "WHERE pronamespace = 'app'::regnamespace ORDER BY 1",
}


def _catalog(engine: Engine) -> dict[str, list[tuple[object, ...]]]:
    with engine.connect() as connection:
        return {
            name: [tuple(row) for row in connection.execute(text(sql))]
            for name, sql in _CATALOG_QUERIES.items()
        }


def test_each_revision_downgrades_to_exactly_the_previous_catalog(
    alembic_config: Config, owner_engine: Engine
) -> None:
    # Single steps, compared on policies (including their expressions), grants
    # (table and column level) and RLS flags, so a downgrade that leaves a
    # policy or privilege behind is caught.
    revisions = [
        script.revision
        for script in reversed(list(ScriptDirectory.from_config(alembic_config).walk_revisions()))
    ]
    assert revisions[-1] == "0009"
    try:
        command.downgrade(alembic_config, "base")
        snapshots = []
        for revision in revisions:
            command.upgrade(alembic_config, revision)
            snapshots.append(_catalog(owner_engine))
        for index in range(len(revisions) - 1, 0, -1):
            command.downgrade(alembic_config, revisions[index - 1])
            assert _catalog(owner_engine) == snapshots[index - 1], revisions[index]
            command.upgrade(alembic_config, revisions[index])
            assert _catalog(owner_engine) == snapshots[index], revisions[index]
            command.downgrade(alembic_config, revisions[index - 1])
    finally:
        command.upgrade(alembic_config, "head")

    assert _catalog(owner_engine) == snapshots[-1]


def test_0004_downgrade_restores_tenant_only_workspace_access(
    alembic_config: Config, owner_engine: Engine
) -> None:
    try:
        command.downgrade(alembic_config, "0003")
        catalog = _catalog(owner_engine)
    finally:
        command.upgrade(alembic_config, "head")
    workspace_policies = [row[1] for row in catalog["policies"] if row[0] == "workspaces"]
    assert workspace_policies == ["tenant_isolation"]
    assert ("app_runtime", "workspaces", "DELETE") in catalog["table_grants"]
    assert ("app_runtime", "workspaces", "name", "UPDATE") in catalog["column_grants"]


def test_models_and_migrations_have_not_drifted(alembic_config: Config) -> None:
    command.check(alembic_config)


def test_drift_detection_notices_a_model_change(owner_engine: Engine) -> None:
    # Guards the gate itself: a column added only in the models must be reported.
    changed = MetaData(naming_convention=Base.metadata.naming_convention)
    for table in Base.metadata.tables.values():
        table.to_metadata(changed)
    changed.tables["workspaces"].append_column(Column("description", Text))

    with owner_engine.connect() as connection:
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, Base.metadata) == []
        diffs = compare_metadata(context, changed)

    assert [(diff[0], diff[3].name) for diff in diffs] == [("add_column", "description")]


def test_migrated_schema_enforces_rls_and_runtime_grants(owner_engine: Engine) -> None:
    with owner_engine.connect() as connection:
        tables = connection.execute(
            text(
                "SELECT relname, pg_get_userbyid(relowner), relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relnamespace = 'app'::regnamespace AND relkind = 'r'"
            )
        ).all()
        policies = connection.execute(
            text(
                "SELECT tablename, policyname, cmd, roles::text "
                "FROM pg_policies WHERE schemaname = 'app'"
            )
        ).all()
        privileges: dict[tuple[str, str], bool] = {
            (table, privilege): connection.execute(
                text("SELECT has_table_privilege('app_runtime', :table, :privilege)"),
                {"table": f"app.{table}", "privilege": privilege},
            ).scalar_one()
            for table in (
                "tenants",
                "workspaces",
                "users",
                "memberships",
                "operator_audit",
                "documents",
                "document_versions",
                "document_contents",
                "document_scans",
                "processing_runs",
                "document_pages",
                "extracted_fields",
                "field_candidates",
                "candidate_evidence",
                "review_decisions",
                "approved_records",
                "alembic_version",
            )
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES")
        }
        column_update: dict[str, bool] = {
            column: connection.execute(
                text("SELECT has_column_privilege('app_runtime', 'app.workspaces', :c, 'UPDATE')"),
                {"c": column},
            ).scalar_one()
            for column in ("id", "tenant_id", "name", "created_at")
        }
        users_column_insert: dict[str, bool] = {
            column: connection.execute(
                text("SELECT has_column_privilege('app_runtime', 'app.users', :c, 'INSERT')"),
                {"c": column},
            ).scalar_one()
            for column in ("id", "issuer", "subject", "created_at")
        }

    assert sorted(tables) == [
        ("alembic_version", "app_owner", False, False),
        ("approved_records", "app_owner", True, True),
        ("candidate_evidence", "app_owner", True, True),
        ("document_contents", "app_owner", True, True),
        ("document_pages", "app_owner", True, True),
        ("document_scans", "app_owner", True, True),
        ("document_versions", "app_owner", True, True),
        ("documents", "app_owner", True, True),
        ("extracted_fields", "app_owner", True, True),
        ("field_candidates", "app_owner", True, True),
        ("memberships", "app_owner", True, True),
        ("operator_audit", "app_owner", True, True),
        ("processing_runs", "app_owner", True, True),
        ("review_decisions", "app_owner", True, True),
        ("tenants", "app_owner", True, True),
        ("users", "app_owner", True, True),
        ("workspaces", "app_owner", True, True),
    ]
    assert sorted(policies) == [
        ("approved_records", "approved_records_admin", "ALL", "{app_owner}"),
        ("approved_records", "approved_records_member_read", "SELECT", "{app_runtime}"),
        ("approved_records", "approved_records_reviewer_create", "INSERT", "{app_runtime}"),
        ("candidate_evidence", "candidate_evidence_admin", "ALL", "{app_owner}"),
        ("candidate_evidence", "candidate_evidence_member_read", "SELECT", "{app_runtime}"),
        ("candidate_evidence", "candidate_evidence_worker_insert", "INSERT", "{app_worker}"),
        ("document_contents", "document_contents_admin", "ALL", "{app_owner}"),
        ("document_contents", "document_contents_member_insert", "INSERT", "{app_runtime}"),
        ("document_contents", "document_contents_member_read", "SELECT", "{app_runtime}"),
        ("document_contents", "document_contents_worker_read", "SELECT", "{app_worker}"),
        ("document_pages", "document_pages_admin", "ALL", "{app_owner}"),
        ("document_pages", "document_pages_member_read", "SELECT", "{app_runtime}"),
        ("document_pages", "document_pages_worker_insert", "INSERT", "{app_worker}"),
        ("document_pages", "document_pages_worker_read", "SELECT", "{app_worker}"),
        ("document_scans", "document_scans_admin", "ALL", "{app_owner}"),
        ("document_scans", "document_scans_member_insert", "INSERT", "{app_runtime}"),
        ("document_scans", "document_scans_member_read", "SELECT", "{app_runtime}"),
        ("document_scans", "document_scans_worker_read", "SELECT", "{app_worker}"),
        ("document_versions", "document_versions_admin", "ALL", "{app_owner}"),
        ("document_versions", "document_versions_member_insert", "INSERT", "{app_runtime}"),
        ("document_versions", "document_versions_member_read", "SELECT", "{app_runtime}"),
        ("document_versions", "document_versions_worker_read", "SELECT", "{app_worker}"),
        ("documents", "documents_admin", "ALL", "{app_owner}"),
        ("documents", "documents_member_insert", "INSERT", "{app_runtime}"),
        ("documents", "documents_member_read", "SELECT", "{app_runtime}"),
        ("extracted_fields", "extracted_fields_admin", "ALL", "{app_owner}"),
        ("extracted_fields", "extracted_fields_member_read", "SELECT", "{app_runtime}"),
        ("extracted_fields", "extracted_fields_worker_insert", "INSERT", "{app_worker}"),
        ("extracted_fields", "extracted_fields_worker_read", "SELECT", "{app_worker}"),
        ("field_candidates", "field_candidates_admin", "ALL", "{app_owner}"),
        ("field_candidates", "field_candidates_member_read", "SELECT", "{app_runtime}"),
        ("field_candidates", "field_candidates_worker_insert", "INSERT", "{app_worker}"),
        ("memberships", "membership_self_read", "SELECT", "{app_runtime}"),
        ("memberships", "memberships_admin", "ALL", "{app_owner}"),
        ("memberships", "memberships_operator", "ALL", "{app_operator}"),
        ("operator_audit", "operator_audit_admin", "ALL", "{app_owner}"),
        ("operator_audit", "operator_audit_append", "INSERT", "{app_operator}"),
        ("operator_audit", "operator_audit_read", "SELECT", "{app_operator}"),
        ("processing_runs", "processing_runs_admin", "ALL", "{app_owner}"),
        ("processing_runs", "processing_runs_member_create", "INSERT", "{app_runtime}"),
        ("processing_runs", "processing_runs_member_read", "SELECT", "{app_runtime}"),
        ("processing_runs", "processing_runs_worker_read", "SELECT", "{app_worker}"),
        ("processing_runs", "processing_runs_worker_update", "UPDATE", "{app_worker}"),
        ("review_decisions", "review_decisions_admin", "ALL", "{app_owner}"),
        ("review_decisions", "review_decisions_member_read", "SELECT", "{app_runtime}"),
        ("review_decisions", "review_decisions_reviewer_create", "INSERT", "{app_runtime}"),
        ("tenants", "tenant_admin", "ALL", "{app_owner}"),
        ("tenants", "tenant_member_read", "SELECT", "{app_runtime}"),
        ("tenants", "tenants_operator", "ALL", "{app_operator}"),
        ("users", "user_by_id", "SELECT", "{app_runtime}"),
        ("users", "user_by_identity", "SELECT", "{app_runtime}"),
        ("users", "user_provision", "INSERT", "{app_runtime}"),
        ("users", "users_admin", "ALL", "{app_owner}"),
        ("users", "users_operator_read", "SELECT", "{app_operator}"),
        ("workspaces", "workspace_member_access", "ALL", "{app_runtime}"),
        ("workspaces", "workspaces_admin", "ALL", "{app_owner}"),
    ]
    granted = {key for key, allowed in privileges.items() if allowed}
    # users, documents, processing_runs and the review tables' INSERT is column-level,
    # so no table-level INSERT appears here. The results are read-only for the runtime,
    # and the review tables are append-only (no UPDATE or DELETE).
    assert granted == {
        ("review_decisions", "SELECT"),
        ("approved_records", "SELECT"),
        ("processing_runs", "SELECT"),
        ("document_pages", "SELECT"),
        ("extracted_fields", "SELECT"),
        ("field_candidates", "SELECT"),
        ("candidate_evidence", "SELECT"),
        ("documents", "SELECT"),
        ("document_versions", "SELECT"),
        ("document_contents", "SELECT"),
        ("document_scans", "SELECT"),
        ("memberships", "SELECT"),
        ("users", "SELECT"),
        ("tenants", "SELECT"),
        ("workspaces", "SELECT"),
        ("workspaces", "INSERT"),
    }
    assert column_update == {"id": False, "tenant_id": False, "name": False, "created_at": False}
    assert users_column_insert == {
        "id": False,
        "issuer": True,
        "subject": True,
        "created_at": False,
    }


def test_0007_keeps_existing_documents_listed_but_unreadable_until_scanned(
    alembic_config: Config,
    owner_engine: Engine,
    runtime_engine: Engine,
    seed_tenant: Callable[..., SeededTenant],
) -> None:
    # A document stored before 0007 has no scan: after the upgrade it is still
    # listed (document and version) but its bytes are not readable; nothing is
    # added or removed. The downgrade makes it readable again, as before.
    a = seed_tenant()
    rows = "SELECT (SELECT count(*) FROM documents WHERE tenant_id = :t), " + ", ".join(
        f"(SELECT count(*) FROM {table} WHERE tenant_id = :t)"
        for table in ("document_versions", "document_contents")
    )

    def visible() -> tuple[int, int]:
        with runtime_engine.connect() as connection:
            connection.begin()
            connection.execute(
                text(
                    "SELECT set_config('app.tenant_id', :t, true), "
                    "set_config('app.user_id', :u, true)"
                ),
                {"t": str(a.ctx.tenant_id), "u": str(a.ctx.user_id)},
            )
            counts = connection.execute(
                text(
                    "SELECT (SELECT count(*) FROM document_versions), "
                    "(SELECT count(*) FROM document_contents)"
                )
            ).one()
            connection.rollback()
        return counts[0], counts[1]

    try:
        command.downgrade(alembic_config, "0006")
        with owner_engine.begin() as connection:
            document_id: UUID = connection.execute(
                text(
                    "INSERT INTO documents (tenant_id, workspace_id, display_name, created_by) "
                    "VALUES (:t, :w, 'legacy.pdf', :u) RETURNING id"
                ),
                {"t": a.ctx.tenant_id, "w": a.workspace_id, "u": a.ctx.user_id},
            ).scalar_one()
            version_id: UUID = connection.execute(
                text(
                    "INSERT INTO document_versions (tenant_id, document_id, version_number, "
                    "media_type, size_bytes, sha256, created_by) "
                    "VALUES (:t, :d, 1, 'application/pdf', 5, :h, :u) RETURNING id"
                ),
                {"t": a.ctx.tenant_id, "d": document_id, "h": b"\x00" * 32, "u": a.ctx.user_id},
            ).scalar_one()
            connection.execute(
                text("INSERT INTO document_contents VALUES (:v, :t, '%PDF-')"),
                {"v": version_id, "t": a.ctx.tenant_id},
            )
        assert visible() == (1, 1)

        command.upgrade(alembic_config, "0007")
        with owner_engine.connect() as connection:
            assert tuple(connection.execute(text(rows), {"t": a.ctx.tenant_id}).one()) == (1, 1, 1)
            assert connection.execute(text("SELECT count(*) FROM document_scans")).scalar_one() == 0
        assert visible() == (1, 0)

        command.downgrade(alembic_config, "0006")
        assert visible() == (1, 1)
    finally:
        command.upgrade(alembic_config, "head")
        with owner_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM document_contents WHERE tenant_id = :t"), {"t": a.ctx.tenant_id}
            )
            connection.execute(
                text("DELETE FROM document_versions WHERE tenant_id = :t"), {"t": a.ctx.tenant_id}
            )
            connection.execute(
                text("DELETE FROM documents WHERE tenant_id = :t"), {"t": a.ctx.tenant_id}
            )
