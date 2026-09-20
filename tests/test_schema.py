from psycopg.errors import GeneratedAlways as psycopg_errors_GeneratedAlways

import pytest


def test_fixture_builds_the_schema(db):
    row = db.execute(
        "SELECT to_regclass('public.unifi_channels')"
    ).fetchone()
    assert row[0] is not None, "unifi_channels was not created"


@pytest.mark.parametrize(
    "code, name, plate, fleet_label, expected",
    [
        # fleet label wins when present
        ("RV10551", "Rover 10551", "WXY 1234", "CAR 1", "CAR 1"),
        # falls back to plate when fleet label is blank
        ("RV10552", "Rover 10552", "WXY 5678", "", "WXY 5678"),
        # falls back to plate when fleet label is NULL
        ("RV10553", "Rover 10553", "WXY 9012", None, "WXY 9012"),
        # the sheet's known blank rows fall all the way through to
        # channel name -- never to an empty string
        ("RV10558", "Rover 10558", "", "", "Rover 10558"),
        ("AF10111", "Affiliate 10111", None, None, "Affiliate 10111"),
        ("RAGT10896", "Reseller 10896", "", None, "Reseller 10896"),
    ],
)
def test_display_name_fallback_chain(db, code, name, plate, fleet_label, expected):
    db.execute(
        "INSERT INTO unifi_channels (channel_code, channel_name, plate, fleet_label)"
        " VALUES (%s, %s, %s, %s)",
        (code, name, plate, fleet_label),
    )
    got = db.execute(
        "SELECT display_name FROM unifi_channels WHERE channel_code = %s", (code,)
    ).fetchone()[0]
    assert got == expected


def test_display_name_is_not_writable(db):
    db.execute(
        "INSERT INTO unifi_channels (channel_code, channel_name) VALUES ('RV1', 'One')"
    )
    with pytest.raises(psycopg_errors_GeneratedAlways):
        db.execute("UPDATE unifi_channels SET display_name = 'nope' WHERE channel_code = 'RV1'")
