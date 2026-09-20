import pytest


@pytest.fixture
def seeded(db):
    db.execute(
        "INSERT INTO unifi_channels (channel_code, channel_name, fleet_label)"
        " VALUES ('RV10551', 'Rover 10551', 'CAR 1')"
    )
    rows = [
        # (order_number, org_code, organization_name, order_status)
        ("O1", "RV10551", "Rover 10551", "Completed"),
        ("O2", "RV10551", "Rover 10551", "completed"),   # case-insensitive
        ("O3", "RV10551", "Rover 10551", "Cancelled"),
        ("O4", "RV10551", "Rover 10551", "Order Voided"),  # substring
        ("O5", "RV10551", "Rover 10551", "Activation Failed"),  # substring
        ("O6", "RV10551", "Rover 10551", "In Progress"),
        ("O7", "RV10551", "Rover 10551", None),          # NULL counts as other
        # an org code nobody has added to the fleet list
        ("O8", "RV99999", "Rover 99999", "In Progress"),
    ]
    for order_number, org, org_name, order_status in rows:
        db.execute(
            "INSERT INTO unifi_orders"
            " (order_number, org_code, organization_name, order_status, created_date)"
            " VALUES (%s, %s, %s, %s, '2026-09-15 10:00+08')",
            (order_number, org, org_name, order_status),
        )
    return db


def test_all_four_views_exist(db):
    names = {
        r[0]
        for r in db.execute(
            "SELECT table_name FROM information_schema.views WHERE table_schema = 'public'"
        ).fetchall()
    }
    assert {
        "unifi_order_status_timeline",
        "unifi_monthly_stats",
        "unifi_monthly_channel_breakdown",
        "unifi_unmapped_channels",
    } <= names


def test_monthly_stats_matches_the_telegram_definitions(seeded):
    row = seeded.execute(
        "SELECT total, completed, cancelled, other FROM unifi_monthly_stats"
    ).fetchone()
    total, completed, cancelled, other = row
    assert total == 8
    assert completed == 2            # 'Completed' and 'completed', exact match only
    assert cancelled == 3            # Cancelled, Order Voided, Activation Failed
    assert other == 3                # In Progress x2, NULL x1
    assert completed + cancelled + other == total


def test_monthly_stats_does_not_count_in_progress_as_completed(seeded):
    # 'completed' is an EXACT match, so a status merely containing the
    # word must not be counted.
    seeded.execute(
        "INSERT INTO unifi_orders (order_number, order_status, created_date)"
        " VALUES ('O9', 'Not Completed', '2026-09-15 10:00+08')"
    )
    completed = seeded.execute("SELECT completed FROM unifi_monthly_stats").fetchone()[0]
    assert completed == 2


def test_unmapped_channels_surfaces_org_codes_with_no_channel_row(seeded):
    rows = seeded.execute(
        "SELECT org_code, organization_name, order_count FROM unifi_unmapped_channels"
    ).fetchall()
    assert rows == [("RV99999", "Rover 99999", 1)]


def test_channel_breakdown_uses_display_name(seeded):
    row = seeded.execute(
        "SELECT channel_display_name, total FROM unifi_monthly_channel_breakdown"
        " WHERE org_code = 'RV10551'"
    ).fetchone()
    assert row == ("CAR 1", 7)


def test_channel_breakdown_falls_back_for_unmapped_codes(seeded):
    row = seeded.execute(
        "SELECT channel_display_name FROM unifi_monthly_channel_breakdown"
        " WHERE org_code = 'RV99999'"
    ).fetchone()
    assert row[0] == "Rover 99999"


def test_timeline_reports_how_long_the_previous_state_held(db):
    db.execute(
        "INSERT INTO unifi_orders (order_number, status) VALUES ('O1', 'Pending')"
    )
    db.execute(
        "UPDATE unifi_order_status_events SET changed_at = '2026-09-01 00:00+08'"
        " WHERE order_number = 'O1'"
    )
    db.execute("UPDATE unifi_orders SET status = 'Active' WHERE order_number = 'O1'")
    db.execute(
        "UPDATE unifi_order_status_events SET changed_at = '2026-09-04 00:00+08'"
        " WHERE order_number = 'O1' AND status = 'Active'"
    )
    rows = db.execute(
        "SELECT status, previous_held_for FROM unifi_order_status_timeline"
        " WHERE order_number = 'O1' ORDER BY changed_at"
    ).fetchall()
    assert rows[0][1] is None            # nothing preceded the first event
    assert rows[1][1].days == 3


def test_timeline_carries_the_channel_display_name(seeded):
    seeded.execute("UPDATE unifi_orders SET status = 'Active' WHERE order_number = 'O1'")
    row = seeded.execute(
        "SELECT channel_display_name FROM unifi_order_status_timeline"
        " WHERE order_number = 'O1' ORDER BY changed_at DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "CAR 1"


def test_channel_display_name_falls_through_empty_organization_name(db):
    # organization_name == '' with no unifi_channels row for the org code:
    # coalesce() alone would let the empty string win over org_code, since
    # coalesce only skips NULL, not ''. The label must fall through to
    # org_code instead of rendering blank.
    db.execute(
        "INSERT INTO unifi_orders"
        " (order_number, org_code, organization_name, order_status, created_date)"
        " VALUES ('O1', 'RV77777', '', 'In Progress', '2026-09-15 10:00+08')"
    )
    row = db.execute(
        "SELECT channel_display_name FROM unifi_order_status_timeline"
        " WHERE order_number = 'O1'"
    ).fetchone()
    assert row[0] == "RV77777"


def test_channel_breakdown_collapses_unmapped_codes_despite_label_drift(db):
    # Two orders share one unmapped org_code in the same month, but the
    # scraped organization_name differs by case/whitespace between them.
    # Grouping by the resolved label (as well as org_code) would split
    # this one rover's counts across two rows; it must stay one row.
    db.execute(
        "INSERT INTO unifi_orders"
        " (order_number, org_code, organization_name, order_status, created_date)"
        " VALUES ('O1', 'RV99999', 'Rover 99999', 'In Progress', '2026-09-15 10:00+08')"
    )
    db.execute(
        "INSERT INTO unifi_orders"
        " (order_number, org_code, organization_name, order_status, created_date)"
        " VALUES ('O2', 'RV99999', 'ROVER 99999 ', 'In Progress', '2026-09-16 10:00+08')"
    )
    rows = db.execute(
        "SELECT org_code, total FROM unifi_monthly_channel_breakdown"
        " WHERE org_code = 'RV99999'"
    ).fetchall()
    assert rows == [("RV99999", 2)]
