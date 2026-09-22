import os
import pathlib
import uuid

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

SQL_FILE = pathlib.Path(__file__).resolve().parent.parent / "sql" / "001_unifi_schema.sql"

ADMIN_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://localhost:5432/postgres",
)


def _with_dbname(url: str, dbname: str) -> str:
    parts = conninfo_to_dict(url)
    parts["dbname"] = dbname
    return make_conninfo(**parts)


@pytest.fixture
def db_url():
    """A throwaway database, dropped on teardown. Yields its URL.

    Separate from `db` so that tests which need to hand a connection
    string to neon_writer get the same database the `db` fixture is
    inspecting.
    """
    name = f"unifi_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _with_dbname(ADMIN_URL, name)
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def db(db_url):
    """An open connection to a fresh database with the schema applied."""
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute(SQL_FILE.read_text())
        yield conn
