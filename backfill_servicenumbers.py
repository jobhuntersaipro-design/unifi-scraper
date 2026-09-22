"""
backfill_servicenumbers.py - Fill the Service Number column for UNI5G orders.

Opens each order's detail, joins prefix+accNbr and pairs it with the SIM's
iccid, exactly as a live scrape would -- the formatting itself is
scrape_orders.select_service_numbers, imported rather than reimplemented so the
two can never drift.

    python backfill_servicenumbers.py Sep 2026
    python backfill_servicenumbers.py Sep 2026 --dry-run
"""

import asyncio
import sys
from typing import Dict, List

import gspread
from dotenv import load_dotenv

load_dotenv()

from gsheets_writer import month_tab_title, open_sheet
from login_manager import login_and_get_context
from scrape_orders import _is_uni5g, select_service_numbers

# Sheets allows ~60 write requests/min. One update_cell per order would spend
# the whole run being throttled, so cells are flushed in batches instead.
BATCH_SIZE = 50


class SessionExpired(RuntimeError):
    """The portal logged us out mid-run."""


def get_orders_needing_service_numbers(ws) -> List[Dict]:
    """Rows whose Package is UNI5G and whose Service Number is still blank."""
    records = ws.get_all_values()
    if not records or len(records) <= 1:
        return []

    headers = records[0]

    def col_idx(name):
        try:
            return headers.index(name)
        except ValueError:
            return -1

    idx_order = col_idx("Order Number")
    idx_package = col_idx("Package")
    idx_service = col_idx("Service Number")

    if idx_order == -1 or idx_package == -1:
        print("  ✗ Missing 'Order Number' or 'Package' column")
        return []
    if idx_service == -1:
        print("  ✗ No 'Service Number' column on this tab — add it beside Package first")
        return []

    orders = []
    for row_num, row in enumerate(records[1:], start=2):
        if not row or len(row) <= idx_order:
            continue

        order_number = row[idx_order].strip().lstrip("'")
        if not order_number:
            continue

        while len(row) <= max(idx_package, idx_service):
            row.append("")

        package = row[idx_package].strip()
        if not _is_uni5g(package) or row[idx_service].strip():
            continue

        orders.append(
            {"row_index": row_num, "order_number": order_number, "package": package}
        )

    return orders


async def fetch_order_items(context, order_id: str) -> List[Dict]:
    """The order's orderItemList, or [] if it could not be read.

    Raises SessionExpired when the portal returns its timeout payload: that is
    HTTP 200 with an error code in the body, so without this check every
    remaining order would be silently recorded as "not found".
    """
    captured = {}

    async def intercept(response):
        try:
            if "getCeeOrderDetail" in response.url and response.status == 200:
                body = await response.json()
                if not isinstance(body, dict):
                    return
                code = str(body.get("code"))
                if code == "200":
                    captured["items"] = (body.get("data") or {}).get(
                        "orderItemList", []
                    ) or []
                elif "session" in str(body.get("message", "")).lower():
                    captured["expired"] = body.get("message")
        except Exception:
            pass

    page = await context.new_page()
    page.on("response", intercept)
    try:
        url = (
            "https://dealer.unifi.com.my/esales/h5/onBoarding/OrderDetails"
            f"?custOrderId={order_id}&custOrderNbr={order_id}"
        )
        await page.goto(url, wait_until="networkidle", timeout=60000)
        for _ in range(20):
            if "items" in captured or "expired" in captured:
                break
            await page.wait_for_timeout(500)
        await page.wait_for_timeout(500)
    except Exception as e:
        print(f"    ⚠️ Error fetching {order_id}: {e}")
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if "expired" in captured:
        raise SessionExpired(captured["expired"])
    return captured.get("items", [])


def _flush(ws, cells, dry_run: bool) -> int:
    if not cells:
        return 0
    if not dry_run:
        ws.update_cells(cells, value_input_option="USER_ENTERED")
    written = len(cells)
    cells.clear()
    return written


async def backfill_service_numbers(month_text: str, year: int, dry_run: bool = False):
    print(f"\n{'=' * 70}")
    print(f"BACKFILL SERVICE NUMBERS - {month_text} {year}"
          f"{'  [DRY RUN]' if dry_run else ''}")
    print(f"{'=' * 70}")

    spread = open_sheet()
    tab_title = month_tab_title(month_text, year)
    try:
        ws = spread.worksheet(tab_title)
    except gspread.exceptions.WorksheetNotFound:
        print(f"  ✗ Tab '{tab_title}' not found")
        return

    orders = get_orders_needing_service_numbers(ws)
    print(f"  UNI5G orders missing Service Number: {len(orders)}")
    if not orders:
        print("  Nothing to backfill")
        return

    service_col = ws.row_values(1).index("Service Number") + 1  # 1-based

    from credential_manager import CredentialManager

    creds = CredentialManager().get_credentials()
    browser, context, pw, page = await login_and_get_context(
        creds["username"], creds["password"]
    )

    pending, filled, empty, written = [], 0, 0, 0
    try:
        for i, order in enumerate(orders, 1):
            order_id = order["order_number"]
            print(f"  [{i}/{len(orders)}] {order_id} ({order['package']})...", end=" ")

            try:
                items = await fetch_order_items(context, order_id)
            except SessionExpired as e:
                print("\n  ✗ Session expired — stopping so the rest are not")
                print(f"    recorded as empty. Re-run to continue. ({e})")
                break

            numbers = select_service_numbers(items, order["package"])
            if numbers:
                pending.append(
                    gspread.Cell(order["row_index"], service_col, numbers)
                )
                filled += 1
                print(f"-> {numbers}")
            else:
                empty += 1
                print("-> none found")

            if len(pending) >= BATCH_SIZE:
                written += _flush(ws, pending, dry_run)
                print(f"    💾 flushed {written}/{filled}")
    finally:
        written += _flush(ws, pending, dry_run)
        await context.close()
        await browser.close()
        await pw.stop()

    verb = "would fill" if dry_run else "filled"
    print(f"\n  {verb} {filled} of {len(orders)} · {empty} had no service number")
    if dry_run:
        print("  (dry run — nothing written)")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv

    month = args[0] if args else "Sep"
    year = int(args[1]) if len(args) > 1 else 2026

    asyncio.run(backfill_service_numbers(month, year, dry_run=dry))
