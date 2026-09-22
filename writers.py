"""Fan one write out to Google Sheets and to Neon.

Sheets is authoritative for the whole dual-write period: it is written
first and its exceptions propagate. Neon is a shadow write -- if it
fails the scrape carries on, and the failure is counted rather than
raised.

Call sites use this module rather than gsheets_writer directly, so that
retiring the sheet later is an edit to one file.
"""

import gsheets_writer
import neon_writer


def upsert_orders(ws, rows):
    """Write a batch of Sheet-shaped row dicts to both stores."""
    gsheets_writer.upsert_rows(ws, rows)
    try:
        neon_writer.upsert_orders(rows)
    except Exception as exc:          # belt and braces; neon_writer guards itself
        print(f"⚠️  neon: shadow write failed: {exc}")


def upsert_order(ws, row):
    """Write a single row to both stores."""
    upsert_orders(ws, [row])
