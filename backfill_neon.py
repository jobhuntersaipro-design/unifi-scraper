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

import os
import sys

from dotenv import load_dotenv

load_dotenv()

import neon_writer
from gsheets_writer import get_all_month_tabs, month_tab_title, open_sheet

CHUNK_SIZE = 500

USAGE = "Usage: python backfill_neon.py [<Month> <Year>]  e.g. python backfill_neon.py Sep 2026"


def rows_from_values(values):
    """Turn a worksheet's raw cell grid into Sheet-shaped row dicts.

    Google Sheets truncates trailing empty cells, so short rows are
    padded rather than skipped. A row with MORE cells than the header
    row is the opposite problem: zip() would silently drop the extras,
    so that case is logged instead of swallowed.
    """
    if not values or len(values) < 2:
        return []

    headers = values[0]
    rows = []
    for sheet_row_num, raw in enumerate(values[1:], start=2):
        if not raw:
            continue
        if len(raw) > len(headers):
            print(
                f"  warning: row {sheet_row_num} has {len(raw)} cells but the "
                f"header row has only {len(headers)}; extra cell(s) dropped: "
                f"{raw[len(headers):]}"
            )
        padded = list(raw) + [""] * (len(headers) - len(raw))
        row = dict(zip(headers, padded))
        if not str(row.get("Order Number", "")).strip().lstrip("'"):
            continue
        rows.append(row)
    return rows


def backfill_tab(ws, chunk_size=CHUNK_SIZE) -> int:
    """Upsert one month tab into Neon. Returns the number of rows actually written.

    upsert_orders() is @_guard-decorated: it can return fewer rows than it
    was handed (or 0) if a write fails. The count returned here must
    reflect what really landed in Neon, not what was merely attempted --
    otherwise a silent DB failure looks identical to a clean run.
    """
    rows = rows_from_values(ws.get_all_values())
    if not rows:
        print(f"  {ws.title}: empty, skipped")
        return 0

    sent = 0
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start:start + chunk_size]
        written = neon_writer.upsert_orders(chunk)
        if written < len(chunk):
            print(
                f"  {ws.title}: only {written}/{len(chunk)} rows in this chunk "
                f"were written -- {len(chunk) - written} did not land"
            )
        sent += written
        print(f"  {ws.title}: {sent}/{len(rows)} written")
    return sent


def main(argv):
    # A backfill exists solely to write to Neon -- unlike neon_writer's
    # library behaviour (which treats an unset DATABASE_URL as a valid
    # "Neon not configured, keep scraping Sheets" state), running this
    # script without a database is always a mistake, never a choice.
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL is not set. This script only writes to Neon; "
              "refusing to run and silently do nothing.")
        return 1

    if len(argv) not in (0, 2):
        print(USAGE)
        return 1

    year = None
    if len(argv) == 2:
        try:
            year = int(argv[1])
        except ValueError:
            print(f"{USAGE}\n('{argv[1]}' is not a valid year)")
            return 1

    spread = open_sheet()

    if argv:
        titles = [month_tab_title(argv[0], year)]
    else:
        titles = get_all_month_tabs(spread)

    print(f"Backfilling {len(titles)} tab(s) into Neon")
    total = 0
    open_failures = 0
    for title in titles:
        try:
            ws = spread.worksheet(title)
        except Exception as exc:
            print(f"  {title}: could not open ({exc})")
            open_failures += 1
            continue
        total += backfill_tab(ws)

    write_failures = neon_writer.write_failure_count()
    print(
        f"\nDone: {total} rows written, {write_failures} Neon write failure(s), "
        f"{open_failures} tab(s) could not be opened"
    )
    if write_failures or open_failures:
        print("Re-run this script — it is idempotent and writes no duplicate events.")
    neon_writer.close()
    return 1 if (write_failures or open_failures) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
