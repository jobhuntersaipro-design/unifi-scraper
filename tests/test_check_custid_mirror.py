import pytest

import check_custid


class FakeWorksheet:
    def __init__(self, fail=False):
        self.batches = []
        self.fail = fail

    def batch_update(self, batch, value_input_option=None):
        if self.fail:
            raise RuntimeError("sheets is down")
        self.batches.append(batch)


class FakeNeon:
    def __init__(self):
        self.updates = []

    def update_cust_ids(self, updates):
        self.updates.extend(updates)
        return len(updates)


@pytest.fixture
def neon(monkeypatch):
    fake = FakeNeon()
    monkeypatch.setattr(check_custid, "neon_writer", fake)
    return fake


def test_write_updates_writes_both_stores(neon):
    ws = FakeWorksheet()
    n = check_custid.write_custid_updates(
        ws, custid_col=19, updates=[(5, "ORD1", "10555", "20999")]
    )
    assert n == 1
    assert len(ws.batches) == 1
    assert neon.updates == [("ORD1", "20999")]


def test_a_failed_sheet_write_is_not_mirrored(neon):
    ws = FakeWorksheet(fail=True)
    with pytest.raises(RuntimeError):
        check_custid.write_custid_updates(
            ws, custid_col=19, updates=[(5, "ORD1", "10555", "20999")]
        )
    assert neon.updates == []


def test_rows_without_an_order_number_are_not_mirrored(neon):
    ws = FakeWorksheet()
    check_custid.write_custid_updates(
        ws, custid_col=19, updates=[(5, "", "10555", "20999")]
    )
    assert len(ws.batches) == 1
    assert neon.updates == []


def test_no_updates_is_a_no_op(neon):
    ws = FakeWorksheet()
    assert check_custid.write_custid_updates(ws, custid_col=19, updates=[]) == 0
    assert ws.batches == []
    assert neon.updates == []
