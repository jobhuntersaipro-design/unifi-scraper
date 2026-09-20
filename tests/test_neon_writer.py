import pytest

import neon_writer


@pytest.fixture
def writer(db, db_url, monkeypatch):
    """Point neon_writer at the same throwaway database `db` inspects."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    neon_writer.close()
    neon_writer.reset_failures()
    yield neon_writer
    neon_writer.close()


def _row(order_number, **over):
    row = {
        "Order Number": order_number,
        "Order Status": "In Progress",
        "Created Date": "22 Oct 2025 09:30",
        "Updated Date": "22 Oct 2025 09:30",
        "Org Code": "RV10551",
        "Organization Name": "Rover 10551",
        "Name": "Ali bin Abu",
        "Address": "12 Jalan Satu",
        "Package": "UNI5G Postpaid 99",
        "Cust ID": "10555",
        "Last Synced": "'2026-09-20 14:30:00",
    }
    row.update(over)
    return row


def test_upsert_inserts_a_new_order(writer, db):
    assert writer.upsert_orders([_row("O1")]) == 1
    row = db.execute(
        "SELECT customer_name, org_code, package FROM unifi_orders WHERE order_number = 'O1'"
    ).fetchone()
    assert row == ("Ali bin Abu", "RV10551", "UNI5G Postpaid 99")


def test_upsert_updates_an_existing_order(writer, db):
    writer.upsert_orders([_row("O1")])
    writer.upsert_orders([_row("O1", **{"Order Status": "Completed"})])
    rows = db.execute("SELECT order_status FROM unifi_orders WHERE order_number = 'O1'").fetchall()
    assert rows == [("Completed",)]


def test_upsert_does_not_clobber_a_status_with_null(writer, db):
    # scrape_orders never checks installation status, so its rows carry
    # no "Status" key. Letting that overwrite would wipe what
    # check_status learned an hour earlier.
    writer.upsert_orders([_row("O1", **{"Status": "Active"})])
    writer.upsert_orders([_row("O1")])          # no Status key at all
    got = db.execute("SELECT status FROM unifi_orders WHERE order_number = 'O1'").fetchone()
    assert got[0] == "Active"


def test_upsert_stores_the_raw_row(writer, db):
    writer.upsert_orders([_row("O1", **{"Device": "Modem X"})])
    got = db.execute("SELECT raw->>'Device' FROM unifi_orders WHERE order_number = 'O1'").fetchone()
    assert got[0] == "Modem X"


def test_upsert_writes_many_rows_in_one_call(writer, db):
    assert writer.upsert_orders([_row(f"O{i}") for i in range(50)]) == 50
    assert db.execute("SELECT count(*) FROM unifi_orders").fetchone()[0] == 50


def test_upsert_skips_rows_with_no_order_number(writer, db):
    writer.upsert_orders([_row("O1"), _row(""), _row(None)])
    assert db.execute("SELECT count(*) FROM unifi_orders").fetchone()[0] == 1


def test_upsert_of_an_empty_list_is_a_no_op(writer):
    assert writer.upsert_orders([]) == 0
    assert writer.write_failure_count() == 0


def test_a_broken_connection_never_raises(monkeypatch):
    # The whole point: Google Sheets is authoritative, and a Neon outage
    # must not fail a scrape.
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/nope")
    monkeypatch.setenv("NEON_POOL_TIMEOUT", "1")   # else this waits 30s
    neon_writer.close()
    neon_writer.reset_failures()
    try:
        assert neon_writer.upsert_orders([_row("O1")]) == 0
        assert neon_writer.write_failure_count() == 1
    finally:
        neon_writer.close()


def test_an_unset_database_url_is_not_counted_as_a_failure(monkeypatch):
    # Running the scraper without Neon configured is a valid state, not
    # an error to be tallied.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    neon_writer.close()
    neon_writer.reset_failures()
    try:
        assert neon_writer.upsert_orders([_row("O1")]) == 0
        assert neon_writer.write_failure_count() == 0
    finally:
        neon_writer.close()
