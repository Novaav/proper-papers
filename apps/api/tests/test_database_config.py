import pytest
from sqlalchemy.exc import OperationalError

from api.database import DatabaseConfigError, DatabaseSettings, create_runtime_engine

SECRET = "s3cr3t-value"


def test_missing_database_url_is_rejected() -> None:
    for env in ({}, {"DATABASE_URL": ""}, {"DATABASE_URL": "   "}):
        with pytest.raises(DatabaseConfigError, match="DATABASE_URL is not set"):
            DatabaseSettings.from_env(env)


@pytest.mark.parametrize(
    "url",
    [
        f"sqlite:///tmp/{SECRET}.db",
        f"postgresql+psycopg2://app:{SECRET}@localhost/app",
        f"postgresql+asyncpg://app:{SECRET}@localhost/app",
        f"mysql://app:{SECRET}@localhost/app",
    ],
)
def test_unsupported_drivers_are_rejected_without_leaking_the_url(url: str) -> None:
    with pytest.raises(DatabaseConfigError, match="is not supported") as error:
        DatabaseSettings.from_env({"DATABASE_URL": url})

    assert SECRET not in str(error.value)


def test_malformed_url_is_rejected_without_echoing_it() -> None:
    with pytest.raises(DatabaseConfigError, match="not a valid database URL") as error:
        DatabaseSettings.from_env({"DATABASE_URL": f"not a url {SECRET}"})

    assert SECRET not in str(error.value)


@pytest.mark.parametrize(
    ("url", "missing"),
    [
        (f"postgresql://app:{SECRET}@/app", "host"),
        (f"postgresql://app:{SECRET}@localhost", "database"),
        ("postgresql://localhost/app", "username"),
    ],
)
def test_incomplete_url_is_rejected(url: str, missing: str) -> None:
    with pytest.raises(DatabaseConfigError, match=f"missing: .*{missing}") as error:
        DatabaseSettings.from_env({"DATABASE_URL": url})

    assert SECRET not in str(error.value)


@pytest.mark.parametrize("scheme", ["postgresql", "postgresql+psycopg"])
def test_postgresql_urls_use_the_psycopg_driver(scheme: str) -> None:
    settings = DatabaseSettings.from_env({"DATABASE_URL": f"{scheme}://app:pw@db:5432/app"})

    assert settings.url.drivername == "postgresql+psycopg"
    assert (settings.url.host, settings.url.port, settings.url.database) == ("db", 5432, "app")


def test_settings_and_engine_representations_hide_the_password() -> None:
    settings = DatabaseSettings.from_env({"DATABASE_URL": f"postgresql://app:{SECRET}@db/app"})
    engine = create_runtime_engine(settings)

    assert SECRET not in repr(settings)
    assert SECRET not in str(settings)
    assert SECRET not in repr(engine)


def test_creating_an_engine_does_not_connect() -> None:
    # Port 1 on localhost refuses connections; only first use may fail.
    settings = DatabaseSettings.from_env({"DATABASE_URL": "postgresql://app:pw@127.0.0.1:1/app"})
    engine = create_runtime_engine(settings)

    with pytest.raises(OperationalError):
        engine.connect()
