import pathlib
import subprocess
import sys

from sql.checksum import compute, header_value

SQL_FILE = pathlib.Path(__file__).resolve().parent.parent / "sql" / "001_unifi_schema.sql"


def test_header_matches_the_body():
    # If this fails, someone edited the DDL without re-running
    # `python -m sql.checksum --write`. The portal's Prisma migration
    # carries the same value; a stale one there means silent drift.
    assert header_value(SQL_FILE) == compute(SQL_FILE), (
        "schema-checksum header is stale -- run: python -m sql.checksum --write"
    )


def test_write_is_idempotent(tmp_path):
    f = tmp_path / "x.sql"
    f.write_text("-- schema-checksum: pending\nSELECT 1;\n")
    subprocess.run([sys.executable, "-m", "sql.checksum", "--write", str(f)], check=True)
    first = f.read_text()
    subprocess.run([sys.executable, "-m", "sql.checksum", "--write", str(f)], check=True)
    assert f.read_text() == first


def test_changing_the_body_changes_the_checksum(tmp_path):
    f = tmp_path / "x.sql"
    f.write_text("-- schema-checksum: pending\nSELECT 1;\n")
    before = compute(f)
    f.write_text("-- schema-checksum: pending\nSELECT 2;\n")
    assert compute(f) != before
