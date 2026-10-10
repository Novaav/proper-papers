"""Alembic environment: migrations run only as the schema owner, never at API startup.

Connection: DATABASE_URL_OWNER (never DATABASE_URL, which is the runtime role), or an
explicit `owner_url` config attribute, which the tests use for the disposable test database.
"""

import os

from alembic import context
from sqlalchemy import create_engine, pool, text

from api.database import DatabaseConfigError, DatabaseSettings
from api.models import Base

# The connected role must own schema `app`, not be a superuser, and resolve
# unqualified names to `app`, so objects are created in the right place with
# the right owner.
_MIGRATION_ROLE_SQL = """
SELECT n.nspowner = r.oid AND NOT r.rolsuper, current_schema() = 'app'
FROM pg_roles AS r, pg_namespace AS n
WHERE r.rolname = current_user AND n.nspname = 'app'
"""


def run_migrations_online() -> None:
    owner_url = context.config.attributes.get("owner_url") or os.environ.get(
        "DATABASE_URL_OWNER", ""
    )
    if not owner_url.strip():
        raise SystemExit("migrations need DATABASE_URL_OWNER (the schema owner's URL)")
    try:
        settings = DatabaseSettings.from_env({"DATABASE_URL": owner_url})
    except DatabaseConfigError as error:
        raise SystemExit(f"DATABASE_URL_OWNER is invalid: {error}") from None

    engine = create_engine(settings.url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        row = connection.execute(text(_MIGRATION_ROLE_SQL)).one_or_none()
        if row is None or not row[0]:
            raise SystemExit("migrations must run as the owner of schema app (not a superuser)")
        if not row[1]:
            raise SystemExit("migration role's search_path must resolve to schema app")
        connection.rollback()

        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline (--sql) migrations are not supported")
run_migrations_online()
