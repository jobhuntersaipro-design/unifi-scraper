import pytest

import backfill_neon

HEADERS = ["Order Number", "Order Status", "Created Date", "Cust ID", "Status"]


class FakeWorksheet:
    def __init__(self, values, title="Sep 2026"):
        self._values = values
        self.title = title

    def get_all_values(self):
        return self._values


def test_rows_from_values_maps_headers_to_dicts():
    rows = backfill_neon.rows_from_values(
        [HEADERS, ["'O1", "Completed", "22 Oct 2025 09:30", "10555", "Active"]]
    )
    assert rows == [
        {
            "Order Number": "'O1",
            "Order Status": "Completed",
            "Created Date": "22 Oct 2025 09:30",
            "Cust ID": "10555",
            "Status": "Active",
        }
    ]


def test_rows_from_values_skips_blank_order_numbers():
    rows = backfill_neon.rows_from_values(
        [HEADERS, ["", "Completed", "", "", ""], ["O2", "", "", "", ""]]
    )
    assert [r["Order Number"] for r in rows] == ["O2"]


def test_rows_from_values_pads_short_rows():
    # Google Sheets truncates trailing empty cells.
    rows = backfill_neon.rows_from_values([HEADERS, ["O1", "Completed"]])
    assert rows[0]["Status"] == ""
    assert rows[0]["Cust ID"] == ""


def test_rows_from_values_handles_an_empty_tab():
    assert backfill_neon.rows_from_values([]) == []
    assert backfill_neon.rows_from_values([HEADERS]) == []


def test_backfill_tab_chunks_its_writes(monkeypatch):
    calls = []
    monkeypatch.setattr(
        backfill_neon.neon_writer, "upsert_orders", lambda rows: calls.append(len(rows))
    )
    values = [HEADERS] + [[f"O{i}", "Completed", "", "", ""] for i in range(1200)]
    n = backfill_neon.backfill_tab(FakeWorksheet(values), chunk_size=500)
    assert n == 1200
    assert calls == [500, 500, 200]
