"""One-time: load every month tab of the Google Sheet into Neon.

Run once at cutover, before the dual-write period begins, so the portal
has real history on day one.

THE TRIGGER STAYS ENABLED. Each row produces exactly one INSERT event
with prev_status NULL, which is precisely the "State at migration" row
the timeline needs. Disabling it would leave every order with no
starting state.

Re-running is safe: the second pass is an UPDATE whose guard finds
nothing meaningfully changed, so no new events appear. That is what
makes this recoverable after a partial failure.

Usage:
  python backfill_neon.py              # every month tab
  python backfill_neon.py Sep 2026     # one tab
"""

import sys

from dotenv import load_dotenv

load_dotenv()

import neon_writer
from gsheets_writer import get_all_month_tabs, month_tab_title, open_sheet

CHUNK_SIZE = 500


def rows_from_values(values):
    """Turn a worksheet's raw cell grid into Sheet-shaped row dicts.

    Google Sheets truncates trailing empty cells, so short rows are
    padded rather than skipped.
    """
    if not values or len(values) < 2:
        return []

    headers = values[0]
    rows = []
    for raw in values[1:]:
        if not raw:
            continue
        padded = list(raw) + [""] * (len(headers) - len(raw))
        row = dict(zip(headers, padded))
        if not str(row.get("Order Number", "")).strip().lstrip("'"):
            continue
        rows.append(row)
    return rows


def backfill_tab(ws, chunk_size=CHUNK_SIZE) -> int:
    """Upsert one month tab into Neon. Returns the number of rows sent."""
    rows = rows_from_values(ws.get_all_values())
    if not rows:
        print(f"  {ws.title}: empty, skipped")
        return 0

    sent = 0
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start:start + chunk_size]
        neon_writer.upsert_orders(chunk)
        sent += len(chunk)
        print(f"  {ws.title}: {sent}/{len(rows)}")
    return sent


def main(argv):
    spread = open_sheet()

    if len(argv) >= 2:
        titles = [month_tab_title(argv[0], int(argv[1]))]
    else:
        titles = get_all_month_tabs(spread)

    print(f"Backfilling {len(titles)} tab(s) into Neon")
    total = 0
    for title in titles:
        try:
            ws = spread.worksheet(title)
        except Exception as exc:
            print(f"  {title}: could not open ({exc})")
            continue
        total += backfill_tab(ws)

    failures = neon_writer.write_failure_count()
    print(f"\nDone: {total} rows sent, {failures} Neon write failure(s)")
    if failures:
        print("Re-run this script — it is idempotent and writes no duplicate events.")
    neon_writer.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
