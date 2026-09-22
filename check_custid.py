"""
check_custid.py - Check if 10XXX custIds have a newer custId via IC lookup.
Dry-run by default (only reports), use --write to update the sheet.

Usage:
  python check_custid.py Jun 2026            # dry run - just report
  python check_custid.py Jun 2026 --write    # update sheet with new custIds
"""

import asyncio
import sys
from typing import Dict, List, Tuple

from dotenv import load_dotenv

load_dotenv()

from check_status import (
    navigate_to_order_entry,
    parse_ic_number,
    query_subs_page_tree,
    query_subscriber_api,
    _all_inactive,
)
from gsheets_writer import HEADERS, month_tab_title, open_sheet
import neon_writer


def get_old_custid_orders(ws) -> Tuple[List[Dict], list]:
    """Find orders with 10XXX custIds that might have newer ones."""
    orders = []
    records = ws.get_all_values()
    if not records or len(records) <= 1:
        return orders, records[0] if records else []

    headers = records[0]

    def col_idx(name):
        try:
            return headers.index(name)
        except ValueError:
            return -1

    idx_order = col_idx("Order Number")
    idx_cust_id = col_idx("Cust ID")
    idx_ic = col_idx("IC Number")
    idx_name = col_idx("Name")
    idx_order_status = col_idx("Order Status")

    if idx_order == -1 or idx_cust_id == -1 or idx_ic == -1:
        print("  Missing required columns")
        return orders, headers

    for row_num, row in enumerate(records[1:], start=2):
        if not row or len(row) <= idx_order:
            continue

        order_number = row[idx_order].strip().lstrip("'")
        if not order_number:
            continue

        # Pad row
        max_idx = max(idx_order, idx_cust_id, idx_ic, idx_name if idx_name != -1 else 0)
        while len(row) <= max_idx:
            row.append("")

        # Skip cancelled orders
        if idx_order_status != -1 and len(row) > idx_order_status:
            if row[idx_order_status].strip().lower() == "cancelled":
                continue

        cust_id = row[idx_cust_id].strip()
        ic_number = row[idx_ic].strip()

        # Only check 10XXX custIds that have an IC number
        if not cust_id or not cust_id.startswith("10") or not ic_number:
            continue

        name = row[idx_name].strip() if idx_name != -1 else ""

        orders.append({
            "row_index": row_num,
            "order_number": order_number,
            "cust_id": cust_id,
            "ic_number": ic_number,
            "name": name,
        })

    return orders, headers


def write_custid_updates(ws, custid_col: int, updates) -> int:
    """Write new cust ids to the sheet, then mirror them into Neon.

    `updates` is [(row_index, order_number, old_custid, new_custid)].

    Sheets is authoritative: if its write raises, the exception
    propagates and nothing is mirrored. cust_id is one of the three
    columns the status trigger watches, so a missed update here is a
    missing timeline event, not just a stale cell -- which is why this
    nightly path mirrors and the one-off backfill_*.py scripts do not.
    """
    if not updates:
        return 0

    from gspread.utils import rowcol_to_a1

    batch = [
        {"range": rowcol_to_a1(row_idx, custid_col), "values": [[new]]}
        for row_idx, _order_number, _old, new in updates
    ]
    ws.batch_update(batch, value_input_option="USER_ENTERED")

    neon_writer.update_cust_ids(
        [(order_number, new) for _row_idx, order_number, _old, new in updates if order_number]
    )
    return len(updates)


async def check_custids_for_month(
    page,
    iframe_frame,
    csrf_token: str,
    month_text: str,
    year: int,
    write: bool = True,
    ws=None,
) -> Dict:
    """
    Check and update 10XXX custIds for a single month.
    Can be called with an existing browser session (from run_daily)
    or standalone.

    Returns dict with stats: {checked, updated, same, errors}
    """
    print(f"\n{'=' * 70}")
    print(f"CHECK CUSTID UPDATES — {month_text} {year}")
    print(f"{'=' * 70}")

    if ws is None:
        spread = open_sheet()
        tab_title = month_tab_title(month_text, year)
        try:
            ws = spread.worksheet(tab_title)
        except Exception:
            print(f"  Tab '{tab_title}' not found — skipping")
            return {"checked": 0, "updated": 0, "same": 0, "errors": 0, "skipped": True}

    orders, headers = get_old_custid_orders(ws)
    print(f"  Orders with 10XXX custId: {len(orders)}")

    if not orders:
        print("  Nothing to check")
        return {"checked": 0, "updated": 0, "same": 0, "errors": 0}

    # Deduplicate by IC number
    ic_groups: Dict[str, List[Dict]] = {}
    for order in orders:
        cert_number, cert_type = parse_ic_number(order["ic_number"])
        if not cert_number:
            continue
        key = f"{cert_number}|{cert_type}"
        if key not in ic_groups:
            ic_groups[key] = []
        ic_groups[key].append(order)

    print(f"  Unique customers to check: {len(ic_groups)}")

    target = iframe_frame
    updated = 0
    same = 0
    errors = 0
    updates = []  # (row_index, order_number, old_custid, new_custid)

    query_num = 0
    for key, group in ic_groups.items():
        query_num += 1
        cert_number, cert_type = key.split("|", 1)
        old_cust_id = group[0]["cust_id"]
        name = group[0].get("name", "")

        print(f"  [{query_num}/{len(ic_groups)}] IC={cert_number} custId={old_cust_id}", end="")

        try:
            ic_results = await query_subscriber_api(
                page, target, csrf_token, cert_number, cert_type, name
            )

            if not ic_results:
                print(" -> no results")
                errors += 1
                continue

            # Find new custIds (non-10XXX)
            new_cust_ids = set()
            for r in ic_results:
                cid = r.get("custId", "")
                if cid and cid != old_cust_id and not cid.startswith("10"):
                    new_cust_ids.add(cid)

            if new_cust_ids:
                # Only write if the new custId actually has subscriber results
                best_new = None
                for new_cid in sorted(new_cust_ids):
                    subs = await query_subs_page_tree(target, csrf_token, new_cid)
                    if subs:
                        best_new = new_cid
                        break

                if best_new:
                    print(f" -> {best_new} ✓")
                    updated += 1
                    for order in group:
                        updates.append(
                            (order["row_index"], order["order_number"], old_cust_id, best_new)
                        )
                else:
                    print(f" -> new IDs found but no subscriber data")
                    same += 1
            else:
                print(" -> same")
                same += 1

        except Exception as e:
            print(f" -> error: {e}")
            errors += 1

    # Summary
    print(f"\n  Checked: {len(ic_groups)}, Updated: {updated}, Same: {same}, Errors: {errors}")
    print(f"  Total rows to update: {len(updates)}")

    # Write updates
    if write and updates:
        custid_col = headers.index("Cust ID") + 1
        write_custid_updates(ws, custid_col, updates)
        print(f"  ✅ Updated {len(updates)} rows in the sheet and Neon")
    elif updates and not write:
        print(f"  Run with --write to apply these updates")

    return {"checked": len(ic_groups), "updated": updated, "same": same, "errors": errors}


async def check_custids_multi_month(
    username: str,
    password: str,
    months: list,
    write: bool = True,
) -> Dict:
    """
    Check custIds for multiple months in one login session.
    Args:
        months: list of (month_text, year) tuples
    """
    from login_manager import login_and_get_context

    browser, context, pw, page = await login_and_get_context(username, password)

    all_results = {}
    try:
        iframe_frame = await navigate_to_order_entry(page)

        # CSRF capture via context.route
        csrf_token = ""
        captured = {}

        async def intercept(route):
            csrf = route.request.headers.get("x-csrf-token", "")
            if csrf:
                captured["token"] = csrf
            await route.continue_()

        await context.route("**/*", intercept)
        try:
            await iframe_frame.evaluate("""() => {
                document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
                document.querySelectorAll('.comprivroot.ui-dialog').forEach(el => {
                    const close = el.querySelector('.ui-dialog-titlebar-close, .close');
                    if (close) close.click();
                    else el.style.display = 'none';
                });
            }""")
            await page.wait_for_timeout(1000)

            await iframe_frame.locator("div.js-advanced-query-btn").first.click(force=True, timeout=15000)
            await page.wait_for_timeout(3000)
            await iframe_frame.locator('input[name="certNbr"]').first.fill("000000000000", timeout=10000)
            await page.wait_for_timeout(300)
            await iframe_frame.locator('input[name="custName"]').first.fill("TEST", timeout=5000)
            await page.wait_for_timeout(300)
            await iframe_frame.locator("button.js-query").first.click(force=True, timeout=5000)
            await page.wait_for_timeout(5000)
        except Exception as e:
            print(f"  CSRF capture error: {e}")
        await context.unroute("**/*")
        csrf_token = captured.get("token", "")

        if not csrf_token:
            print("  CSRF token not captured!")
            return all_results

        for month_text, year in months:
            try:
                result = await check_custids_for_month(
                    page, iframe_frame, csrf_token, month_text, year, write=write
                )
                all_results[f"{month_text} {year}"] = result
            except Exception as e:
                print(f"  Error checking {month_text} {year}: {e}")
                all_results[f"{month_text} {year}"] = {"error": str(e)}

    finally:
        await context.close()
        await browser.close()
        await pw.stop()

    # Overall summary
    print(f"\n{'=' * 70}")
    print("CUSTID CHECK COMPLETE")
    print(f"{'=' * 70}")
    total_updated = 0
    for key, result in all_results.items():
        if "error" in result:
            print(f"  {key}: ERROR - {result['error']}")
        else:
            u = result.get('updated', 0)
            total_updated += u
            print(f"  {key}: {result.get('checked', 0)} checked, {u} updated, {result.get('errors', 0)} errors")
    print(f"  Total updated: {total_updated}")
    print(f"{'=' * 70}")

    return all_results


async def check_custids_standalone(month_text: str, year: int, write: bool = False):
    """Standalone entry point for a single month."""
    from credential_manager import CredentialManager

    creds = CredentialManager().get_credentials()
    await check_custids_multi_month(
        creds["username"], creds["password"],
        [(month_text, year)],
        write=write,
    )


if __name__ == "__main__":
    month = sys.argv[1] if len(sys.argv) > 1 else "Jun"
    year = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
    write = "--write" in sys.argv

    asyncio.run(check_custids_standalone(month, year, write=write))
