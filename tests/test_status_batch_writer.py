import pytest

import check_status
from check_status import StatusBatchWriter

HEADERS = [
    "Order Number", "Event Type", "Order Status", "Created Date", "Updated Date",
    "Org Code", "Organization Name", "Name", "Company Name", "Email",
    "Phone Number", "Appointment Date", "Address", "Package", "Device",
    "IC Number", "Creator", "Last Synced", "Cust ID", "Status",
    "Status Latest Date", "Status Scrape Date",
]


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

    def update_order_statuses(self, updates):
        self.updates.extend(updates)
        return len(updates)


@pytest.fixture
def neon(monkeypatch):
    fake = FakeNeon()
    monkeypatch.setattr(check_status, "neon_writer", fake)
    return fake


def test_flush_mirrors_the_batch_to_neon(neon):
    ws = FakeWorksheet()
    writer = StatusBatchWriter(ws, HEADERS)
    writer.add(5, "ORD1", "Active", "22 Oct 2025")
    writer.flush()

    assert len(ws.batches) == 1
    assert len(neon.updates) == 1
    assert neon.updates[0].order_number == "ORD1"
    assert neon.updates[0].status == "Active"
    assert neon.updates[0].status_latest_date == "22 Oct 2025"


def test_flush_carries_a_new_cust_id(neon):
    ws = FakeWorksheet()
    writer = StatusBatchWriter(ws, HEADERS)
    writer.add(5, "ORD1", "Active", "22 Oct 2025", new_cust_id="20999")
    writer.flush()
    assert neon.updates[0].new_cust_id == "20999"


def test_a_failed_sheet_write_is_not_mirrored(neon):
    # Neon must never claim something the authoritative store rejected.
    ws = FakeWorksheet(fail=True)
    writer = StatusBatchWriter(ws, HEADERS)
    writer.add(5, "ORD1", "Active", "22 Oct 2025")
    writer.flush()
    assert neon.updates == []
    assert writer.write_failures == 1


def test_the_cancelled_sentinel_is_mirrored_with_its_order_number(neon):
    ws = FakeWorksheet()
    writer = StatusBatchWriter(ws, HEADERS)
    writer.add(5, "ORD1", "-")
    writer.flush()
    assert neon.updates[0].order_number == "ORD1"
    assert neon.updates[0].status == "-"


def test_a_row_with_no_order_number_still_writes_to_the_sheet(neon):
    # The sheet is keyed by row index and does not need the order
    # number; only the Neon mirror does.
    ws = FakeWorksheet()
    writer = StatusBatchWriter(ws, HEADERS)
    writer.add(5, "", "Active")
    writer.flush()
    assert len(ws.batches) == 1
    assert neon.updates == []


def test_flush_empties_the_pending_batch(neon):
    ws = FakeWorksheet()
    writer = StatusBatchWriter(ws, HEADERS)
    writer.add(5, "ORD1", "Active")
    writer.flush()
    writer.flush()
    assert len(ws.batches) == 1
    assert len(neon.updates) == 1
