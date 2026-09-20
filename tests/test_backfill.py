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

    def fake_upsert_orders(rows):
        calls.append(len(rows))
        return len(rows)

    monkeypatch.setattr(backfill_neon.neon_writer, "upsert_orders", fake_upsert_orders)
    values = [HEADERS] + [[f"O{i}", "Completed", "", "", ""] for i in range(1200)]
    n = backfill_neon.backfill_tab(FakeWorksheet(values), chunk_size=500)
    assert n == 1200
    assert calls == [500, 500, 200]


def test_backfill_tab_counts_only_rows_actually_written(monkeypatch):
    # upsert_orders is @_guard-decorated: it can return fewer rows than it
    # was handed (or 0) when a write fails partway through a chunk. The
    # total backfill_tab reports must reflect what actually landed in
    # Neon, not what was merely attempted -- otherwise a silent DB failure
    # looks identical to a clean run.
    monkeypatch.setattr(
        backfill_neon.neon_writer, "upsert_orders", lambda rows: len(rows) - 1
    )
    values = [HEADERS] + [[f"O{i}", "Completed", "", "", ""] for i in range(3)]
    n = backfill_neon.backfill_tab(FakeWorksheet(values), chunk_size=500)
    assert n == 2


def test_backfill_tab_returns_zero_when_every_write_fails(monkeypatch):
    monkeypatch.setattr(backfill_neon.neon_writer, "upsert_orders", lambda rows: 0)
    values = [HEADERS] + [[f"O{i}", "Completed", "", "", ""] for i in range(1200)]
    n = backfill_neon.backfill_tab(FakeWorksheet(values), chunk_size=500)
    assert n == 0


def test_rows_from_values_warns_on_a_row_longer_than_the_headers(capsys):
    # zip() silently drops extra trailing cells; an operator scanning
    # output needs to know a row was truncated on write, not just that
    # short rows were padded.
    rows = backfill_neon.rows_from_values(
        [HEADERS, ["O1", "Completed", "22 Oct 2025 09:30", "10555", "Active", "extra"]]
    )
    assert rows == [
        {
            "Order Number": "O1",
            "Order Status": "Completed",
            "Created Date": "22 Oct 2025 09:30",
            "Cust ID": "10555",
            "Status": "Active",
        }
    ]
    captured = capsys.readouterr()
    assert "warning" in captured.out.lower()


def test_main_requires_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert backfill_neon.main([]) == 1


def test_main_rejects_a_single_argument(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/nope")
    assert backfill_neon.main(["Sep"]) == 1


def test_main_rejects_a_malformed_year(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/nope")
    assert backfill_neon.main(["Sep", "not-a-year"]) == 1
