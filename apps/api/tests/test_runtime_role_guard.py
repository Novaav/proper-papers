"""The runtime role guard also refuses roles that are members of privileged roles."""

import secrets
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Engine, text

from api.database import DatabaseRoleError, DatabaseSettings, create_runtime_engine

MakeRole = Callable[[str], DatabaseSettings]


@pytest.fixture
def member_role(admin_engine: Engine, runtime_settings: DatabaseSettings) -> Iterator[MakeRole]:
    """Create temporary login roles that are members of a given role; always dropped."""
    created: list[str] = []

    def make(parent: str) -> DatabaseSettings:
        name = f"guard_probe_{secrets.token_hex(6)}"
        password = secrets.token_hex(16)
        created.append(name)
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    f"CREATE ROLE {name} LOGIN PASSWORD '{password}' "
                    f"NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE IN ROLE {parent}"
                )
            )
            connection.execute(text(f"GRANT CONNECT ON DATABASE app_test TO {name}"))
        url = runtime_settings.url.set(username=name, password=password)
        return DatabaseSettings.from_env(
            {"DATABASE_URL": url.render_as_string(hide_password=False)}
        )

    yield make

    with admin_engine.connect() as connection:
        for name in reversed(created):
            connection.execute(text(f"DROP OWNED BY {name}"))
            connection.execute(text(f"DROP ROLE {name}"))


def _connects(settings: DatabaseSettings) -> bool:
    engine = create_runtime_engine(settings)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except DatabaseRoleError:
        return False
    finally:
        engine.dispose()


def test_member_of_the_schema_owner_is_refused(member_role: MakeRole) -> None:
    assert not _connects(member_role("app_owner"))


def test_indirect_member_of_the_schema_owner_is_refused(member_role: MakeRole) -> None:
    intermediate = member_role("app_owner").url.username
    assert not _connects(member_role(str(intermediate)))


def test_member_of_a_superuser_role_is_refused(member_role: MakeRole) -> None:
    assert not _connects(member_role("postgres"))


def test_member_of_an_unprivileged_role_is_accepted(member_role: MakeRole) -> None:
    assert _connects(member_role("app_runtime"))
