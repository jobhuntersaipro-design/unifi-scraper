# gsheets_writer.py - Month-based tabs version
import os
from datetime import datetime
from typing import Dict, List

import gspread
from gspread.utils import rowcol_to_a1

# UPDATED HEADERS with Org Code and Organization Name
HEADERS = [
    "Order Number",
    "Event Type",
    "Order Status",
    "Created Date",
    "Updated Date",
    "Org Code",
    "Organization Name",
    "Name",
    "Company Name",
    "Email",
    "Phone Number",
    "Appointment Date",
    "Address",
    "Package",
    "Service Number",
    "Device",
    "IC Number",
    "Creator",
    "Last Synced",
    "Cust ID",
    "Status",
    "Status Latest Date",
    "Status Scrape Date",
]

MONTH_ORDER = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]


def open_sheet():
    sid = os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID")
    if not sid:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID not set")
    gc = gspread.service_account(filename="service_account.json")
    # Without a timeout, a slow/hung Sheets API call blocks forever (read timeout=None).
    # Cap it so requests fail fast and the caller's retry/backoff can recover.
    gc.set_timeout(120)
    return gc.open_by_key(sid)


def today_tab_title():
    """Return current month as tab title (e.g., 'Oct')"""
    return datetime.now().strftime("%b")  # Short month name: Jan, Feb, etc.


def month_tab_title(month_text: str, year: int = None):
    """
    Return tab title for a specific month
    Args:
        month_text: Month name (e.g., 'Oct', 'October')
        year: Optional year (if provided, returns 'Oct 2025' format)
    Returns:
        Tab title string
    """
    # Normalize month name to short format (Jan, Feb, etc.)
    month_map = {
        "January": "Jan",
        "Jan": "Jan",
        "February": "Feb",
        "Feb": "Feb",
        "March": "Mar",
        "Mar": "Mar",
        "April": "Apr",
        "Apr": "Apr",
        "May": "May",
        "June": "Jun",
        "Jun": "Jun",
        "July": "Jul",
        "Jul": "Jul",
        "August": "Aug",
        "Aug": "Aug",
        "September": "Sep",
        "Sep": "Sep",
        "October": "Oct",
        "Oct": "Oct",
        "November": "Nov",
        "Nov": "Nov",
        "December": "Dec",
        "Dec": "Dec",
    }

    short_month = month_map.get(month_text, month_text)

    if year:
        return f"{short_month} {year}"
    return short_month


def parse_month_tab(tab_title: str):
    """
    Parse tab title to month index for sorting
    Returns (month_index, year) tuple or None
    Examples: 'Oct' -> (10, None), 'Oct 2025' -> (10, 2025)
    """
    try:
        parts = tab_title.strip().split()
        month_name = parts[0]

        if month_name in MONTH_ORDER:
            month_idx = MONTH_ORDER.index(month_name)
            year = int(parts[1]) if len(parts) > 1 else None
            return (month_idx, year)
        return None
    except:
        return None


def ensure_tabs_sorted_by_month(spread):
    """
    Ensure worksheet tabs are sorted chronologically by month
    Jan → Feb → Mar → ... → Dec (or with years: Jan 2024 → Feb 2024 → ... → Dec 2025)
    """
    try:
        worksheets = spread.worksheets()

        # Separate month tabs from other tabs
        month_tabs = []
        other_tabs = []

        for ws in worksheets:
            parsed = parse_month_tab(ws.title)
            if parsed:
                month_idx, year = parsed
                # Sort key: (year or 0, month_index)
                sort_key = (year if year else 0, month_idx)
                month_tabs.append((ws, sort_key))
            else:
                other_tabs.append(ws)

        # Sort month tabs chronologically (reversed: newest first)
        month_tabs.sort(key=lambda x: x[1], reverse=True)

        # Reorder worksheets if needed
        target_order = [ws for ws, _ in month_tabs] + other_tabs
        current_order = worksheets

        if [ws.title for ws in target_order] != [ws.title for ws in current_order]:
            print("📊 Reordering tabs by month (newest first)...")

            # Reorder worksheets
            for i, ws in enumerate(target_order):
                try:
                    ws.reorder(i)
                except:
                    pass  # Ignore reorder errors

            print(
                f"✅ Tabs ordered: {' → '.join([ws.title for ws in target_order[:5]])}{'...' if len(target_order) > 5 else ''}"
            )

    except Exception as e:
        print(f"⚠️ Warning: Could not sort tabs by month: {e}")


def ensure_tab(spread, title: str):
    try:
        ws = spread.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = spread.add_worksheet(title=title, rows=500, cols=len(HEADERS))
        ws.append_row(HEADERS)
        # Format ONLY header row: bold text
        ws.format("1:1", {"textFormat": {"bold": True}, "horizontalAlignment": "LEFT"})

    # ensure headers present
    first = ws.row_values(1)
    if first != HEADERS:
        # Update headers if they don't match (e.g. adding new columns)
        ws.update([HEADERS], "A1")
        # Format ONLY header row: bold text
        ws.format("1:1", {"textFormat": {"bold": True}, "horizontalAlignment": "LEFT"})

    # Format column A (order_number) as plain text to prevent scientific notation
    ws.format("A:A", {"numberFormat": {"type": "TEXT"}})

    # Ensure data rows are NOT bold (reset formatting for rows 2 and beyond)
    try:
        # Get the last row with data
        all_values = ws.get_all_values()
        if len(all_values) > 1:  # If there are data rows beyond header
            last_row = len(all_values)
            # Format data rows as normal (not bold)
            data_range = f"2:{last_row}"
            ws.format(
                data_range,
                {"textFormat": {"bold": False}, "horizontalAlignment": "LEFT"},
            )
    except:
        pass  # Don't fail if we can't reset formatting

    return ws


def build_index(ws) -> Dict[str, int]:
    """
    Return dict of order_number -> row_index (1-based).
    Strips legacy apostrophes for backwards compatibility.
    """
    records = ws.get_all_values()
    idx = {}
    for i in range(1, len(records)):  # skip header (row 0 in list = row 1 in sheet)
        row = records[i]
        if not row:
            continue
        key = row[0].strip() if row and len(row) >= 1 else ""
        if key:
            # Remove apostrophe prefix if it exists (backwards compatibility)
            key = key.lstrip("'")
            idx[key] = i + 1  # sheet row (1-based)
    return idx


def upsert_rows(ws, rows: List[Dict[str, str]]):
    """
    Upsert by order_number.
    For existing: update entire row.
    For new: append.
    Ensures data rows are formatted as normal (not bold).
    Ensures Order Number is formatted as TEXT to prevent scientific notation.
    """
    index = build_index(ws)
    updates = []
    to_append = []

    for r in rows:
        # ensure all headers present
        values = [str(r.get(h, "") or "") for h in HEADERS]

        # Get order number for key lookup (no apostrophe prefix needed - column is formatted as TEXT)
        key = values[0].strip().lstrip("'")  # Remove any legacy apostrophes
        if not key:
            continue

        if key in index:
            row_num = index[key]
            # Calculate range covering all columns including new ones
            last_col_char = rowcol_to_a1(row_num, len(HEADERS)).split(":")[0]
            # rowcol_to_a1 returns like A1, B5. We just need column letters roughly,
            # but simplest is to just ask gspread for the A1 notation
            cell_range_a1 = rowcol_to_a1(row_num, len(HEADERS))
            # If row_num is 5, cell_range_a1 is something like 'O5' (15th col).
            # We want 'A5:O5'.
            rng = f"A{row_num}:{cell_range_a1}"

            # Simple fallback if complex calculation fails, but standard gspread utils work well
            updates.append((rng, [values]))
        else:
            to_append.append(values)

    # batch updates
    if updates:
        ws.batch_update(
            [{"range": rng, "values": vals} for rng, vals in updates],
            value_input_option="USER_ENTERED",
        )

        # Make sure updated rows are NOT bold
        for rng, _ in updates:
            try:
                ws.format(
                    rng, {"textFormat": {"bold": False}, "horizontalAlignment": "LEFT"}
                )
            except:
                pass

    if to_append:
        # Get current last row before appending
        current_rows = len(ws.get_all_values())

        ws.append_rows(to_append, value_input_option="USER_ENTERED")

        # Format the newly appended rows as NOT bold
        try:
            new_start_row = current_rows + 1
            new_end_row = current_rows + len(to_append)
            if new_end_row >= new_start_row:
                new_range = f"{new_start_row}:{new_end_row}"
                ws.format(
                    new_range,
                    {"textFormat": {"bold": False}, "horizontalAlignment": "LEFT"},
                )
        except Exception as e:
            # If formatting fails, don't crash the whole operation
            print(f"⚠️ Could not format new rows: {e}")


def fix_existing_formatting(ws):
    """
    Fix formatting for existing sheet - make headers bold, data normal
    Call this once to fix an existing sheet's formatting
    """
    try:
        print("🎨 Fixing sheet formatting...")

        # Make header row bold
        ws.format("1:1", {"textFormat": {"bold": True}, "horizontalAlignment": "LEFT"})

        # Get all data and make data rows normal
        all_values = ws.get_all_values()
        if len(all_values) > 1:
            last_row = len(all_values)
            data_range = f"2:{last_row}"
            ws.format(
                data_range,
                {"textFormat": {"bold": False}, "horizontalAlignment": "LEFT"},
            )
            print(f"✅ Fixed formatting: Headers bold, {last_row-1} data rows normal")
        else:
            print("✅ Only headers present, made them bold")

    except Exception as e:
        print(f"⚠️ Error fixing formatting: {e}")


def get_all_month_tabs(spread) -> List[str]:
    """Get all worksheet tabs that match month format, sorted newest first (Dec → Jan)"""
    worksheets = spread.worksheets()
    month_tabs = []

    for ws in worksheets:
        parsed = parse_month_tab(ws.title)
        if parsed:
            month_idx, year = parsed
            sort_key = (year if year else 0, month_idx)
            month_tabs.append((ws.title, sort_key))

    # Sort chronologically reversed (newest first)
    month_tabs.sort(key=lambda x: x[1], reverse=True)
    return [title for title, _ in month_tabs]


def sort_tab_by_created_date(ws, descending=True):
    """
    Sort worksheet by Created Date column (column C / index 2)
    Args:
        ws: Worksheet object
        descending: True = newest first (default), False = oldest first
    """
    try:
        print(f"🔄 Sorting tab '{ws.title}' by Created Date...")

        # Get all data
        all_values = ws.get_all_values()

        if len(all_values) <= 1:
            print("  ℹ️ No data rows to sort")
            return

        # Separate header and data rows
        header = all_values[0]
        data_rows = all_values[1:]

        # Find Created Date column index by header name
        try:
            created_date_idx = header.index("Created Date")
        except ValueError:
            created_date_idx = 2  # fallback

        # Sort data rows by Created Date
        # Parse dates for proper sorting
        def parse_date_for_sort(row):
            """Parse date string for sorting. Returns tuple for comparison."""
            try:
                if len(row) <= created_date_idx or not row[created_date_idx]:
                    return (0, 0, 0, 0, 0, 0)  # Empty dates go to end

                date_str = row[created_date_idx].strip()
                # Format: "31 Oct 2025 14:29:30" or "31 Oct 2025 14:29"
                from datetime import datetime

                for fmt in ["%d %b %Y %H:%M:%S", "%d %b %Y %H:%M"]:
                    try:
                        dt = datetime.strptime(date_str, fmt)
                        return (
                            dt.year,
                            dt.month,
                            dt.day,
                            dt.hour,
                            dt.minute,
                            dt.second,
                        )
                    except:
                        continue
                return (0, 0, 0, 0, 0, 0)
            except:
                return (0, 0, 0, 0, 0, 0)

        sorted_rows = sorted(data_rows, key=parse_date_for_sort, reverse=descending)

        # Combine header + sorted data
        sorted_data = [header] + sorted_rows

        # Clear sheet
        ws.clear()

        # CRITICAL: Format column A as TEXT BEFORE writing data
        # This prevents scientific notation for large order numbers
        ws.format("A:A", {"numberFormat": {"type": "TEXT"}})

        # Now write sorted data
        ws.update(sorted_data, value_input_option="USER_ENTERED")

        # Re-apply formatting
        # Headers bold
        ws.format("1:1", {"textFormat": {"bold": True}, "horizontalAlignment": "LEFT"})

        # Data rows normal
        if len(sorted_data) > 1:
            ws.format(
                f"2:{len(sorted_data)}",
                {"textFormat": {"bold": False}, "horizontalAlignment": "LEFT"},
            )

        print(
            f"  ✅ Sorted {len(data_rows)} rows by Created Date ({'newest first' if descending else 'oldest first'})"
        )

    except Exception as e:
        print(f"  ⚠️ Error sorting tab: {e}")


def sort_all_month_tabs(spread, descending=True):
    """
    Sort all month tabs by Created Date
    """
    try:
        month_tabs = get_all_month_tabs(spread)

        if not month_tabs:
            print("No month tabs found to sort")
            return

        print(f"\n📊 Sorting {len(month_tabs)} month tabs by Created Date...")

        for tab_title in month_tabs:
            try:
                ws = spread.worksheet(tab_title)
                sort_tab_by_created_date(ws, descending)
            except Exception as e:
                print(f"  ⚠️ Error sorting {tab_title}: {e}")

        print(f"✅ All tabs sorted\n")

    except Exception as e:
        print(f"⚠️ Error in sort_all_month_tabs: {e}")
