def _events(db, order_number="ORD1"):
    return db.execute(
        "SELECT prev_order_status, order_status, prev_status, status, cust_id"
        "  FROM unifi_order_status_events"
        " WHERE order_number = %s ORDER BY id",
        (order_number,),
    ).fetchall()


def test_insert_writes_one_event_with_null_previous(db):
    # prev_* NULL is what the portal renders as "State at migration".
    db.execute(
        "INSERT INTO unifi_orders (order_number, order_status, status)"
        " VALUES ('ORD1', 'In Progress', 'Pending')"
    )
    rows = _events(db)
    assert len(rows) == 1
    assert rows[0] == (None, "In Progress", None, "Pending", None)


def test_insert_then_status_update_produces_exactly_two_events(db):
    # Acceptance criterion 2, first half.
    db.execute(
        "INSERT INTO unifi_orders (order_number, order_status, status)"
        " VALUES ('ORD1', 'In Progress', 'Pending')"
    )
    db.execute("UPDATE unifi_orders SET status = 'Active' WHERE order_number = 'ORD1'")
    rows = _events(db)
    assert len(rows) == 2
    assert rows[1][2] == "Pending"
    assert rows[1][3] == "Active"


def test_repeating_the_same_update_produces_no_event(db):
    # Acceptance criterion 2, second half.
    db.execute(
        "INSERT INTO unifi_orders (order_number, order_status, status)"
        " VALUES ('ORD1', 'In Progress', 'Pending')"
    )
    db.execute("UPDATE unifi_orders SET status = 'Active' WHERE order_number = 'ORD1'")
    before = len(_events(db))
    db.execute("UPDATE unifi_orders SET status = 'Active' WHERE order_number = 'ORD1'")
    db.execute("UPDATE unifi_orders SET status = 'Active' WHERE order_number = 'ORD1'")
    assert len(_events(db)) == before


def test_scrape_date_moving_alone_produces_no_event(db):
    # This is the one that matters nightly: check_status stamps
    # status_scrape_date and last_synced on EVERY order it verifies,
    # whether or not anything changed. If those were in the guard, one
    # quiet night would write one event per order.
    db.execute(
        "INSERT INTO unifi_orders (order_number, order_status, status)"
        " VALUES ('ORD1', 'In Progress', 'Active')"
    )
    before = len(_events(db))
    db.execute(
        "UPDATE unifi_orders"
        "   SET status_scrape_date = now(), last_synced = now()"
        " WHERE order_number = 'ORD1'"
    )
    assert len(_events(db)) == before


def test_cust_id_change_is_an_event(db):
    # check_custid.py rewrites 10XXX cust ids nightly; a swap is a real
    # observation and the portal shows it beside the status transition.
    db.execute(
        "INSERT INTO unifi_orders (order_number, status, cust_id)"
        " VALUES ('ORD1', 'Active', '10555')"
    )
    db.execute("UPDATE unifi_orders SET cust_id = '20999' WHERE order_number = 'ORD1'")
    rows = _events(db)
    assert len(rows) == 2
    assert rows[1][4] == "20999"


def test_order_status_change_is_an_event(db):
    db.execute(
        "INSERT INTO unifi_orders (order_number, order_status) VALUES ('ORD1', 'In Progress')"
    )
    db.execute(
        "UPDATE unifi_orders SET order_status = 'Completed' WHERE order_number = 'ORD1'"
    )
    rows = _events(db)
    assert len(rows) == 2
    assert rows[1][0] == "In Progress"
    assert rows[1][1] == "Completed"


def test_unrelated_column_change_produces_no_event(db):
    db.execute(
        "INSERT INTO unifi_orders (order_number, address) VALUES ('ORD1', 'Old address')"
    )
    before = len(_events(db))
    db.execute("UPDATE unifi_orders SET address = 'New address' WHERE order_number = 'ORD1'")
    assert len(_events(db)) == before


def test_event_picks_up_the_run_id_when_one_is_set(db):
    db.execute("SELECT set_config('unifi.scrape_run_id', '42', false)")
    db.execute("INSERT INTO unifi_orders (order_number, status) VALUES ('ORD1', 'Active')")
    got = db.execute(
        "SELECT scrape_run_id FROM unifi_order_status_events WHERE order_number = 'ORD1'"
    ).fetchone()[0]
    assert got == 42


def test_event_run_id_is_null_when_unset(db):
    db.execute("INSERT INTO unifi_orders (order_number, status) VALUES ('ORD1', 'Active')")
    got = db.execute(
        "SELECT scrape_run_id FROM unifi_order_status_events WHERE order_number = 'ORD1'"
    ).fetchone()[0]
    assert got is None
