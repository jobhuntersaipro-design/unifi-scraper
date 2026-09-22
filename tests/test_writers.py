import pytest

import writers


class FakeSheets:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def upsert_rows(self, ws, rows):
        if self.fail:
            raise RuntimeError("sheets is down")
        self.calls.append((ws, rows))


class FakeNeon:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def upsert_orders(self, rows):
        if self.fail:
            raise RuntimeError("neon is down")
        self.calls.append(rows)
        return len(rows)


@pytest.fixture
def fakes(monkeypatch):
    sheets, neon = FakeSheets(), FakeNeon()
    monkeypatch.setattr(writers, "gsheets_writer", sheets)
    monkeypatch.setattr(writers, "neon_writer", neon)
    return sheets, neon


def test_upsert_order_writes_to_both(fakes):
    sheets, neon = fakes
    writers.upsert_order("WS", {"Order Number": "O1"})
    assert sheets.calls == [("WS", [{"Order Number": "O1"}])]
    assert neon.calls == [[{"Order Number": "O1"}]]


def test_sheets_is_written_first(fakes):
    # If Sheets rejects a row, Neon must not claim it.
    sheets, neon = fakes
    sheets.fail = True
    with pytest.raises(RuntimeError, match="sheets is down"):
        writers.upsert_order("WS", {"Order Number": "O1"})
    assert neon.calls == []


def test_a_neon_failure_does_not_reach_the_caller(monkeypatch):
    # neon_writer guards itself, but the facade must not reintroduce a
    # raise if that guard is ever bypassed.
    sheets, neon = FakeSheets(), FakeNeon(fail=True)
    monkeypatch.setattr(writers, "gsheets_writer", sheets)
    monkeypatch.setattr(writers, "neon_writer", neon)
    writers.upsert_order("WS", {"Order Number": "O1"})     # must not raise
    assert len(sheets.calls) == 1


def test_upsert_orders_passes_the_whole_batch(fakes):
    sheets, neon = fakes
    rows = [{"Order Number": "O1"}, {"Order Number": "O2"}]
    writers.upsert_orders("WS", rows)
    assert sheets.calls == [("WS", rows)]
    assert neon.calls == [rows]
