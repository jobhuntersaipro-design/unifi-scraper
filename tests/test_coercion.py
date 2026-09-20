from datetime import datetime

import pytest

from neon_writer import LOCAL_TZ, coerce_row, parse_dt, status_text, text


@pytest.mark.parametrize(
    "raw, expected",
    [
        # what format_datetime() and standardize_date() emit
        ("22 Oct 2025 09:30", datetime(2025, 10, 22, 9, 30)),
        ("01 Dec 2025 13:00:00", datetime(2025, 12, 1, 13, 0, 0)),
        # what _extract_status_date() emits -- DATE ONLY, no time
        ("22 Oct 2025", datetime(2025, 10, 22, 0, 0)),
        # what the Last Synced column holds, apostrophe and all
        ("'2026-09-20 14:30:00", datetime(2026, 9, 20, 14, 30, 0)),
        ("2026-09-20 14:30:00", datetime(2026, 9, 20, 14, 30, 0)),
        # the API format, when _extract_status_date could not parse it
        ("2025/10/22 09:30:00", datetime(2025, 10, 22, 9, 30, 0)),
        # the raw dealer-portal format
        ("20251022093000", datetime(2025, 10, 22, 9, 30, 0)),
    ],
)
def test_parse_dt_handles_every_format_the_scraper_emits(raw, expected):
    got = parse_dt(raw)
    assert got == expected.replace(tzinfo=LOCAL_TZ)


def test_parse_dt_attaches_the_local_timezone():
    # Every date the scraper writes is naive Kuala Lumpur time. Storing
    # it without a zone would shift every timestamp by 8 hours.
    got = parse_dt("22 Oct 2025 09:30")
    assert got.tzinfo is not None
    assert got.utcoffset().total_seconds() == 8 * 3600


@pytest.mark.parametrize("raw", ["", "   ", None, "not a date", "N/A", "-"])
def test_parse_dt_returns_none_rather_than_raising(raw):
    # A garbage cell must not kill a scrape.
    assert parse_dt(raw) is None


def test_text_strips_the_sheet_apostrophe():
    # Order numbers are written as "'12345" so Sheets treats them as
    # text instead of rendering 1.2345e4.
    assert text("'1234567890123") == "1234567890123"


@pytest.mark.parametrize("raw, expected", [("  x  ", "x"), ("", None), ("   ", None), (None, None)])
def test_text_blanks_become_none(raw, expected):
    assert text(raw) == expected


def test_status_text_maps_the_cancelled_sentinel_to_none():
    # "-" is the sheet's "cancelled, don't check" marker. It was never an
    # observed state, and storing it literally would fill the churn
    # matrix with transitions into "-".
    assert status_text("-") is None
    assert status_text("Active") == "Active"
    assert status_text("") is None


def test_coerce_row_maps_every_sheet_header():
    row = {
        "Order Number": "'1234567890",
        "Event Type": "New Install",
        "Order Status": "Completed",
        "Created Date": "22 Oct 2025 09:30",
        "Updated Date": "23 Oct 2025 11:00",
        "Org Code": "RV10551",
        "Organization Name": "Rover 10551",
        "Name": "Ali bin Abu",
        "Company Name": "",
        "Email": "ali@example.com",
        "Phone Number": "0123456789",
        "Appointment Date": "25 Oct 2025 14:00",
        "Address": "12 Jalan Satu",
        "Package": "UNI5G Postpaid 99",
        "Device": "Modem X",
        "IC Number": "900101015555 (MyKad)",
        "Creator": "Siti (S123)",
        "Last Synced": "'2026-09-20 14:30:00",
        "Cust ID": "10555",
        "Status": "Active",
        "Status Latest Date": "22 Oct 2025",
        "Status Scrape Date": "'2026-09-20 14:30:00",
    }
    got = coerce_row(row)

    assert got["order_number"] == "1234567890"
    assert got["customer_name"] == "Ali bin Abu"      # "Name" -> customer_name
    assert got["company_name"] is None                # blank -> NULL
    assert got["org_code"] == "RV10551"
    assert got["status"] == "Active"
    assert got["created_date"] == parse_dt("22 Oct 2025 09:30")
    assert got["status_latest_date"] == parse_dt("22 Oct 2025")
    assert got["raw"] == row                          # plain dict, wrapped later


def test_coerce_row_keeps_the_whole_row_in_raw():
    row = {"Order Number": "1", "Some Future Column": "value"}
    assert coerce_row(row)["raw"]["Some Future Column"] == "value"


def test_coerce_row_defaults_last_synced_to_now():
    got = coerce_row({"Order Number": "1"})
    assert got["last_synced"] is not None
    assert got["last_synced"].tzinfo is not None


def test_coerce_row_tolerates_missing_keys():
    # check_status and the backfill both hand over partial rows.
    got = coerce_row({"Order Number": "1"})
    assert got["order_number"] == "1"
    assert got["address"] is None
    assert got["created_date"] is None
