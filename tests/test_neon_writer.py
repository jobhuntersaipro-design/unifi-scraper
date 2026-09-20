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


def test_status_update_sets_status_and_scrape_date(writer, db):
    writer.upsert_orders([_row("O1")])
    n = writer.update_order_statuses(
        [writer.StatusUpdate("O1", "Active", "22 Oct 2025", "")]
    )
    assert n == 1
    row = db.execute(
        "SELECT status, status_latest_date IS NOT NULL, status_scrape_date IS NOT NULL"
        "  FROM unifi_orders WHERE order_number = 'O1'"
    ).fetchone()
    assert row == ("Active", True, True)


def test_status_update_writes_a_timeline_event(writer, db):
    writer.upsert_orders([_row("O1")])
    writer.update_order_statuses([writer.StatusUpdate("O1", "Active", "22 Oct 2025", "")])
    rows = db.execute(
        "SELECT prev_status, status FROM unifi_order_status_events"
        " WHERE order_number = 'O1' ORDER BY id"
    ).fetchall()
    assert rows[-1] == (None, "Active")


def test_repeating_a_status_check_writes_no_new_event(writer, db):
    # The nightly case. status_scrape_date moves every time; the event
    # log must not.
    writer.upsert_orders([_row("O1")])
    writer.update_order_statuses([writer.StatusUpdate("O1", "Active", "22 Oct 2025", "")])
    before = db.execute("SELECT count(*) FROM unifi_order_status_events").fetchone()[0]
    writer.update_order_statuses([writer.StatusUpdate("O1", "Active", "22 Oct 2025", "")])
    writer.update_order_statuses([writer.StatusUpdate("O1", "Active", "22 Oct 2025", "")])
    after = db.execute("SELECT count(*) FROM unifi_order_status_events").fetchone()[0]
    assert after == before


def test_the_cancelled_sentinel_lands_as_null(writer, db):
    writer.upsert_orders([_row("O1")])
    writer.update_order_statuses([writer.StatusUpdate("O1", "-", "", "")])
    got = db.execute("SELECT status FROM unifi_orders WHERE order_number = 'O1'").fetchone()
    assert got[0] is None


def test_status_update_can_carry_a_new_cust_id(writer, db):
    writer.upsert_orders([_row("O1")])
    writer.update_order_statuses([writer.StatusUpdate("O1", "Active", "22 Oct 2025", "20999")])
    got = db.execute("SELECT cust_id FROM unifi_orders WHERE order_number = 'O1'").fetchone()
    assert got[0] == "20999"


def test_blank_cust_id_does_not_wipe_the_existing_one(writer, db):
    writer.upsert_orders([_row("O1")])
    writer.update_order_statuses([writer.StatusUpdate("O1", "Active", "22 Oct 2025", "")])
    got = db.execute("SELECT cust_id FROM unifi_orders WHERE order_number = 'O1'").fetchone()
    assert got[0] == "10555"


def test_update_cust_ids_rewrites_and_logs_an_event(writer, db):
    writer.upsert_orders([_row("O1")])
    before = db.execute("SELECT count(*) FROM unifi_order_status_events").fetchone()[0]
    assert writer.update_cust_ids([("O1", "20999")]) == 1
    got = db.execute("SELECT cust_id FROM unifi_orders WHERE order_number = 'O1'").fetchone()
    assert got[0] == "20999"
    after = db.execute("SELECT count(*) FROM unifi_order_status_events").fetchone()[0]
    assert after == before + 1


def test_update_cust_ids_ignores_unknown_orders(writer, db):
    # check_custid works from sheet rows; an order missing from Neon is
    # not an error, it just has not been backfilled yet.
    assert writer.update_cust_ids([("NOPE", "20999")]) == 1
    assert db.execute("SELECT count(*) FROM unifi_orders").fetchone()[0] == 0


def test_status_updates_of_an_empty_list_is_a_no_op(writer):
    assert writer.update_order_statuses([]) == 0
    assert writer.update_cust_ids([]) == 0
    assert writer.write_failure_count() == 0


def test_start_run_returns_an_id_and_marks_it_running(writer, db):
    run_id = writer.start_run("Sep", 2026, "incremental", "cron")
    assert isinstance(run_id, int)
    row = db.execute(
        "SELECT month_text, year, scrape_mode, triggered_by, status"
        "  FROM unifi_scrape_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert row == ("Sep", 2026, "incremental", "cron", "running")


def test_finish_run_records_the_counts(writer, db):
    run_id = writer.start_run("Sep", 2026, "incremental", "cron")
    writer.finish_run(
        run_id, "done",
        counts={"orders_processed": 412, "successful": 409, "skipped": 0, "failed": 3},
    )
    row = db.execute(
        "SELECT status, orders_processed, failed, finished_at IS NOT NULL"
        "  FROM unifi_scrape_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert row == ("done", 412, 3, True)


def test_finish_run_records_an_error(writer, db):
    run_id = writer.start_run("Sep", 2026, "full", "cron")
    writer.finish_run(run_id, "error", error="login timed out")
    row = db.execute(
        "SELECT status, error FROM unifi_scrape_runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert row == ("error", "login timed out")


def test_events_written_during_a_run_carry_its_id(writer, db):
    run_id = writer.start_run("Sep", 2026, "incremental", "cron")
    writer.upsert_orders([_row("O1")])
    got = db.execute(
        "SELECT scrape_run_id FROM unifi_order_status_events WHERE order_number = 'O1'"
    ).fetchone()[0]
    assert got == run_id
    writer.finish_run(run_id, "done")


def test_events_written_outside_a_run_have_no_id(writer, db):
    writer.upsert_orders([_row("O1")])
    got = db.execute(
        "SELECT scrape_run_id FROM unifi_order_status_events WHERE order_number = 'O1'"
    ).fetchone()[0]
    assert got is None


def test_finish_run_clears_the_current_run(writer, db):
    run_id = writer.start_run("Sep", 2026, "incremental", "cron")
    writer.finish_run(run_id, "done")
    writer.upsert_orders([_row("O2")])
    got = db.execute(
        "SELECT scrape_run_id FROM unifi_order_status_events WHERE order_number = 'O2'"
    ).fetchone()[0]
    assert got is None


def test_start_run_without_a_database_returns_none(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    neon_writer.close()
    neon_writer.reset_failures()
    try:
        assert neon_writer.start_run("Sep", 2026, "incremental", "cron") is None
        neon_writer.finish_run(None, "done")     # must not raise
        assert neon_writer.write_failure_count() == 0
    finally:
        neon_writer.close()
