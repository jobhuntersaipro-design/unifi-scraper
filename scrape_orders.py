"""
Pure UI scraper with improved incremental sync based on Updated Date vs Last Synced
Supports both CSV export (Telegram) and Google Sheets (daily)
"""

import csv
import json
import os
from datetime import datetime, time
from typing import Dict, List, Optional, Tuple

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from date_utils import month_range_yyyymmddhhmmss, standardize_date
import writers
from gsheets_writer import (
    ensure_tab,
    ensure_tabs_sorted_by_month,
    month_tab_title,
    open_sheet,
)
from login_manager import login_and_get_context

OUTPUT_DIR = "outputs"
os.makedirs(OUTPUT_DIR, exist_ok=True)


def format_datetime(datetime_str):
    """Convert 20251022093000 to '22 Oct 2025 09:30'"""
    if not datetime_str or len(datetime_str) != 14:
        return ""
    try:
        dt = datetime.strptime(datetime_str, "%Y%m%d%H%M%S")
        return dt.strftime("%d %b %Y %H:%M")
    except:
        return datetime_str


async def close_blocking_popup(page: Page):
    """Checks for and closes blocking modals using multiple strategies."""
    try:
        # Check if a modal wrapper is visible
        modal_wrap = page.locator(".ant-modal-wrap")
        # We use .first because sometimes multiple wrappers exist in the DOM (even if hidden)
        if await modal_wrap.count() > 0 and await modal_wrap.first.is_visible():
            print("🛡️ Blocking modal detected. Attempting to close...")

            # Strategy 1: The specific "Later" button (Your known case)
            later_btn = page.locator('button.ant-btn:has-text("Later")')
            if await later_btn.count() > 0 and await later_btn.first.is_visible():
                print("  - ✅ Found 'Later' button. Clicking it...")
                await later_btn.first.click()
                await page.wait_for_timeout(1500)
                return

            # Strategy 2: The standard Ant Design "X" close icon
            # It usually has the class .ant-modal-close or .ant-modal-close-x
            close_icon = page.locator(".ant-modal-close")
            if await close_icon.count() > 0 and await close_icon.first.is_visible():
                print("  - ❎ Found 'X' close icon. Clicking it...")
                await close_icon.first.click()
                await page.wait_for_timeout(1000)
                return

            # Strategy 3: Generic "Cancel" or "Close" text buttons
            cancel_btn = page.locator(
                'button.ant-btn:has-text("Cancel"), button.ant-btn:has-text("Close")'
            )
            if await cancel_btn.count() > 0 and await cancel_btn.first.is_visible():
                print("  - 🚫 Found Cancel/Close button. Clicking it...")
                await cancel_btn.first.click()
                await page.wait_for_timeout(1000)
                return

            # Strategy 4: Click the background mask (The dark overlay)
            # This works if 'maskClosable' is enabled
            print("  - 🖱️ Clicking background mask (0,0)...")
            await page.mouse.click(1, 1)  # Click top-left corner of screen
            await page.wait_for_timeout(1000)
        else:
            print("  - No blocking popups found.")

    except Exception as e:
        print(f"  - ⚠️ Error while trying to close popup: {e}")


def parse_ui_date(date_str):
    """Parse date from UI format '29 Oct 2025 11:27:31' to datetime object"""
    if not date_str:
        return None
    try:
        # Handle different possible formats
        for fmt in [
            "%d %b %Y %H:%M:%S",
            "%d %b %Y %H:%M",
            "%d-%m-%Y %H:%M:%S",
            "%d-%m-%Y %H:%M",
        ]:
            try:
                return datetime.strptime(date_str, fmt)
            except:
                continue
        return None
    except:
        return None


from datetime import datetime

try:
    from zoneinfo import ZoneInfo  # Py3.9+
except ImportError:
    ZoneInfo = None  # Fallback if needed

LOCAL_TZ = ZoneInfo("Asia/Kuala_Lumpur") if ZoneInfo else None
UTC_TZ = ZoneInfo("UTC") if ZoneInfo else None


def _to_utc(dt: datetime) -> datetime:
    # If no tzinfo, assume local (MYT) then convert to UTC
    if dt.tzinfo is None:
        if LOCAL_TZ:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt if not UTC_TZ else dt.astimezone(UTC_TZ)
    return dt if not UTC_TZ else dt.astimezone(UTC_TZ)


def parse_last_synced(last_synced_str: str):
    """Parse 'Last Synced' into a timezone-aware UTC datetime.
    Accepts ISO with T or space, optional Z/offsets, and common human formats.
    Returns None only if completely unparsable."""
    if not last_synced_str:
        return None

    s = last_synced_str.strip()

    # Normalize trailing Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    # 1) Try Python's ISO parser (handles both 'T' and offsets; also works for 'YYYY-MM-DD HH:MM:SS' on 3.11+)
    try:
        dt = datetime.fromisoformat(s)
        return _to_utc(dt)
    except Exception:
        pass

    # 2) Try common variants
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%d %b %Y %H:%M:%S",  # e.g., 02 Nov 2025 21:28:52
        "%d %b %Y %H:%M",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            return _to_utc(dt)
        except Exception:
            continue

    # 3) Last resort: be forgiving about a single space instead of T
    if " " in s and "T" not in s:
        try:
            dt = datetime.fromisoformat(s.replace(" ", "T"))
            return _to_utc(dt)
        except Exception:
            pass

    return None


async def click_and_select_all_agents(page) -> int:
    """Click Filter, expand Created by, select all agents with pagination support"""
    print("\n🎯 Selecting agents from UI...")

    await page.click('button:has-text("Filter")', timeout=15000)
    await page.wait_for_timeout(5000)

    # Expand "Created by" section
    print("  📂 Expanding 'Created by' section...")
    try:
        await page.evaluate(
            '() => document.querySelector("span.icon-ic_nav_expand").click()'
        )
        await page.wait_for_timeout(1500)
    except:
        await page.click("span.icon-ic_nav_expand", force=True, timeout=5000)
        await page.wait_for_timeout(1500)

    # Open channel modal
    print("  🖼️ Opening channel selection modal...")
    try:
        await page.click('img[src*="chooseChannel"]', timeout=5000)
        await page.wait_for_timeout(2000)
    except:
        await page.click('img[alt*="channel"]', timeout=3000)
        await page.wait_for_timeout(2000)

    # Set 50/page in modal to minimize clicking "Next"
    try:
        # Targeting the dropdown specifically inside the modal
        modal_dropdown = page.locator(".ant-modal-body .ant-select-selection--single")
        if await modal_dropdown.count() > 0:
            await modal_dropdown.first.click()
            await page.wait_for_timeout(1000)
            await page.click('.ant-select-dropdown-menu-item:has-text("50 / page")')
            await page.wait_for_timeout(2000)
    except Exception as e:
        print(f"  ⚠️ Could not set modal pagination: {e}")

    total_selected = 0
    page_num = 1

    while True:
        # Wait for table rows in the modal
        await page.wait_for_selector(
            ".ant-modal-body tr.ant-table-row[data-row-key]", timeout=10000
        )
        channel_rows = await page.locator(
            ".ant-modal-body tr.ant-table-row[data-row-key]"
        ).all()

        print(f"  📋 Page {page_num}: Selecting {len(channel_rows)} agents...")

        for row in channel_rows:
            try:
                # We check if it's already selected by looking for a class or checkbox state if applicable
                # but clicking usually toggles or selects in this UI
                await row.click()
                total_selected += 1
                await page.wait_for_timeout(50)
            except:
                pass

        # Check for Pagination inside the modal
        next_btn = page.locator(".ant-modal-body li.ant-pagination-next")

        # If the button doesn't exist or is marked as 'disabled'
        is_disabled = await next_btn.get_attribute("aria-disabled")
        if await next_btn.count() == 0 or is_disabled == "true":
            print(f"  ✅ Finished selecting all {total_selected} agents.")
            break

        print(f"  ➡️ Moving to next page of agents...")
        await next_btn.click()
        await page.wait_for_timeout(3000)  # Wait for table to refresh
        page_num += 1

    # Click the final Select button to confirm
    await page.click(
        'button:has-text("Select"):not(:has-text("Select All"))', timeout=5000
    )
    await page.wait_for_timeout(2000)

    return total_selected


async def check_existing_orders_with_dates(
    ws,
) -> Tuple[Dict[str, datetime], Dict[str, int], set]:
    """
    Check Google Sheet for existing orders, their dates, and MISSING DATA.
    Returns:
        - complete_orders: {order_id: last_synced_datetime}
        - incomplete_orders: {order_id: row_index}
        - orders_missing_org: set(order_ids) -> IDs that have Last Synced but no Org Code
    """
    complete_orders = {}
    incomplete_orders = {}
    orders_missing_org = set()

    try:
        records = ws.get_all_values()
        if not records:
            return {}, {}, set()

        headers = records[0]
        print(f"  📊 Checking {len(records)-1} existing orders...")

        # Dynamically find "Org Code" column index (safer than hardcoding)
        try:
            org_code_idx = headers.index("Org Code")
        except ValueError:
            org_code_idx = -1  # Column not found

        for idx, row in enumerate(records[1:], start=2):
            if not row or len(row) == 0:
                continue

            order_number = row[0].strip() if len(row) > 0 and row[0] else ""
            if not order_number:
                continue

            order_number = order_number.lstrip("'")

            # Check if Org Code is missing
            has_org_code = False
            if org_code_idx != -1 and len(row) > org_code_idx:
                if row[org_code_idx].strip():
                    has_org_code = True

            # Existing Last Synced check (assuming it's still around column 12 or 14)
            # We try to find "Last Synced" header, fallback to index 12/14 if needed
            try:
                ls_idx = headers.index("Last Synced")
                last_synced = row[ls_idx].strip() if len(row) > ls_idx else ""
            except ValueError:
                # Fallback to your original hardcoded index if header missing
                last_synced = row[13].strip() if len(row) > 13 and row[13] else ""

            if last_synced.strip():
                last_synced_dt = parse_last_synced(last_synced)
                complete_orders[order_number] = last_synced_dt or last_synced.strip()

                # If it's synced but missing Org Code, mark for rescrape
                if not has_org_code:
                    orders_missing_org.add(order_number)
            else:
                incomplete_orders[order_number] = idx

        return complete_orders, incomplete_orders, orders_missing_org

    except Exception as e:
        print(f"⚠️ Error checking orders: {e}")
        return {}, {}, set()


def should_rescrape_order(
    order_id: str, ui_updated_date: str, complete_orders: Dict[str, datetime]
) -> bool:
    """
    Determine if order should be re-scraped.
    If Last Synced exists, skip completely (don't open modal).
    """
    # If order has Last Synced timestamp, skip it completely
    if order_id in complete_orders:
        return False  # Already has Last Synced - skip completely

    return True  # No Last Synced - needs scraping


async def scrape_orders_month(
    username: str,
    password: str,
    month_text: str,
    year: int,
    output_format: str = "sheets",
    csv_filename: Optional[str] = None,
    full_sync: bool = True,  # NEW: Set to True to capture everything, False for smart sync
    check_status: bool = False,  # Run subscriber status check after scraping
) -> Dict:
    """
    Scrape orders by clicking Details and capturing API response
    With smart incremental sync AND full sync capabilities

    Args:
        full_sync: If True, scrapes ALL orders (ignores existing data)
                  If False, uses smart incremental sync (default)
    """

    sync_mode = "FULL CAPTURE" if full_sync else "SMART INCREMENTAL"
    print("\n" + "=" * 70)
    print(f"UNIFI SCRAPER ({sync_mode})")
    print("=" * 70)
    print(f"Month: {month_text} {year}")
    print(f"Output: {output_format.upper()}")
    print(
        f"Mode: {'Full Sync (all orders)' if full_sync else 'Incremental (new/updated only)'}"
    )
    print("=" * 70)

    browser, context, pw, page = await login_and_get_context(username, password)

    try:
        created_from, created_to = month_range_yyyymmddhhmmss(month_text, year)

        # Prepare output
        if output_format == "sheets":
            spread = open_sheet()
            tab_title = month_tab_title(month_text, year)
            ws = ensure_tab(spread, tab_title)

            # Ensure tabs are sorted by month (chronological)
            ensure_tabs_sorted_by_month(spread)

            print(f"📊 Google Sheets tab: {tab_title}")
            ws = spread.worksheet(tab_title)
            all_orders = []
        else:
            all_orders = []
            if not csv_filename:
                csv_filename = f"unifi_orders_{month_text}_{year}_{datetime.now(LOCAL_TZ).strftime('%Y%m%d_%H%M%S')}.csv"
            csv_path = os.path.join(OUTPUT_DIR, csv_filename)
            checkpoint_file = csv_path.replace(".csv", "_checkpoint.json")
            print(f"📄 CSV file: {csv_path}")
            ws = None

        # Navigate to History
        print("\n📑 Navigating to History tab...")
        print(f"  📍 Current URL: {page.url}")
        await page.screenshot(path="logs/before_history_click.png")

        # Retry clicking History tab — the page may still be loading
        history_clicked = False
        for attempt in range(3):
            try:
                await page.locator('text="History"').last.click(timeout=15000)
                history_clicked = True
                print(f"  ✅ History tab clicked (attempt {attempt + 1})")
                await page.wait_for_timeout(10000)
                break
            except Exception as e:
                print(f"  ⚠️ Attempt {attempt + 1} failed: {e}")
                await page.wait_for_timeout(5000)

        if not history_clicked:
            print("  ❌ All History tab attempts failed")
            await page.screenshot(path="logs/history_tab_failed.png")

        # Set month filter with YEAR support
        try:
            print(f"🗓️ Setting month to {month_text} {year}...")

            await close_blocking_popup(page)

            # Click to open date picker
            await page.click(".ant-picker .ant-picker-input", timeout=15000)
            await page.wait_for_timeout(5000)

            # Navigate to correct year
            current_year = datetime.now(LOCAL_TZ).year
            year_diff = year - current_year

            if year_diff < 0:
                # Need to go back in time - click previous year button
                print(f"  ⏪ Clicking previous year button {abs(year_diff)} time(s)...")
                for _ in range(abs(year_diff)):
                    await page.click(
                        "button.ant-picker-header-super-prev-btn", timeout=5000
                    )
                    await page.wait_for_timeout(500)
            elif year_diff > 0:
                # Need to go forward in time - click next year button
                print(f"  ⏩ Clicking next year button {year_diff} time(s)...")
                for _ in range(year_diff):
                    await page.click(
                        "button.ant-picker-header-super-next-btn", timeout=5000
                    )
                    await page.wait_for_timeout(500)
            else:
                print(f"  ✅ Already on {year}")

            # Now select the month
            await page.click(
                f'td.ant-picker-cell:has-text("{month_text}")', timeout=20000
            )
            await page.wait_for_timeout(1000)
            print(f"  ✅ Set to {month_text} {year}")

        except Exception as e:
            print(f"  ⚠️ Month filter failed: {e}")

        # Select agents
        agent_count = await click_and_select_all_agents(page)

        # Click Query
        print("\n🔍 Clicking Query...")
        try:
            await page.evaluate(
                """() => {
                const buttons = Array.from(document.querySelectorAll('button'));
                const queryBtn = buttons.find(btn => btn.textContent.includes('Query'));
                if (queryBtn) queryBtn.click();
            }"""
            )
        except:
            await page.click('button:has-text("Query")', force=True)

        await page.wait_for_timeout(3000)

        # Set pagination to 50/page
        print("📄 Setting results to 50/page...")
        try:
            # Wait for table to load with visible state
            await page.wait_for_selector(
                "table tbody tr", timeout=45000, state="visible"
            )
            await page.wait_for_timeout(2000)

            # Count current rows BEFORE changing pagination
            initial_row_count = await page.locator("tbody tr.ant-table-row").count()
            print(f"  📊 Initial rows visible: {initial_row_count}")

            all_pag = page.locator('.ant-select-selection--single[role="combobox"]')
            count = await all_pag.count()

            if count > 0:
                last_pag = all_pag.last
                current_text = await last_pag.text_content()

                if "10" in current_text:
                    print(f"  Current: {current_text}, changing to 50/page...")
                    await last_pag.click()
                    await page.wait_for_timeout(2000)
                    await page.click(
                        '.ant-select-dropdown-menu-item:has-text("50 / page")'
                    )

                    # Wait for page to reload
                    print("⏳ Waiting for page to reload...")
                    await page.wait_for_timeout(45000)

                    # Count rows after change
                    new_row_count = await page.locator("tbody tr.ant-table-row").count()
                    print(f"  ✅ Rows after change: {new_row_count}")

                    # FIX: Don't wait for 50 rows if we have fewer
                    final_count = await page.locator("tbody tr.ant-table-row").count()
                    print(f"  ✅ Loaded {final_count} rows")

        except Exception as e:
            print(f"  ⚠️ Pagination setup failed: {e}")
            print(f"  ℹ️ Continuing with current pagination...")

        # Get existing orders with their last synced dates
        if output_format == "sheets":
            print("\n🔍 Checking existing data with date comparison...")
            complete_orders, incomplete_orders, orders_missing_org = (
                await check_existing_orders_with_dates(ws)
            )

            if complete_orders:
                print(f"  ✅ {len(complete_orders)} orders with sync dates")
            if orders_missing_org:
                print(
                    f"  ⚠️ {len(orders_missing_org)} orders missing Org Code (will rescrape)"
                )
            if incomplete_orders:
                print(f"  🔄 {len(incomplete_orders)} incomplete orders")
        else:
            # CSV mode: Use checkpoint
            complete_orders = {}
            incomplete_orders = {}

            if os.path.exists(checkpoint_file):
                try:
                    with open(checkpoint_file, "r") as f:
                        checkpoint = json.load(f)
                        # Convert to datetime objects for CSV mode too
                        for order_id, last_synced_str in checkpoint.get(
                            "completed", {}
                        ).items():
                            last_synced_dt = parse_last_synced(last_synced_str)
                            if last_synced_dt:
                                complete_orders[order_id] = last_synced_dt
                        incomplete_orders = checkpoint.get("incomplete", {})

                    print(f"\n🔍 Checkpoint found:")
                    if complete_orders:
                        print(f"  ✅ {len(complete_orders)} completed with dates")
                    if incomplete_orders:
                        print(f"  🔄 {len(incomplete_orders)} incomplete")
                except:
                    pass

            # Load partial CSV
            if os.path.exists(csv_path):
                try:
                    import csv as csv_lib

                    with open(csv_path, "r", encoding="utf-8") as f:
                        reader = csv_lib.DictReader(f)
                        all_orders = list(reader)
                    print(f"  📄 Loaded {len(all_orders)} orders from CSV")
                except:
                    all_orders = []

        # Setup API interception
        captured_details = {}
        processed_orders = set()  # Track orders we've already processed/failed

        # Prevent duplicate logs/processing within a run
        seen_ids = set()

        async def intercept_response(response):
            """Capture order detail API responses"""
            try:
                url = response.url
                if "getCeeOrderDetail" in url and response.status == 200:
                    try:
                        json_data = await response.json()
                        data = json_data.get("data", {})
                        order_number = data.get("custOrderNbr", "")
                        if order_number:
                            captured_details[order_number] = json_data
                            print(f"\n    📡 API response captured for {order_number}")
                    except:
                        pass
            except:
                pass

        page.on("response", intercept_response)

        # Reusable detail page — created once, reused for all orders
        detail_page = None

        async def fetch_order_json_via_api(order_id: str) -> dict:
            """Fetch order detail by reusing a single page (avoids creating/destroying pages)."""
            nonlocal detail_page
            captured = {}

            async def _intercept(resp):
                try:
                    if "getCeeOrderDetail" in resp.url and resp.status == 200:
                        jd = await resp.json()
                        if isinstance(jd, dict):
                            data = jd.get("data", {})
                            cust_nbr = (
                                data.get("custOrderNbr")
                                or data.get("orderId")
                                or ""
                            )
                            if str(cust_nbr).strip() == str(order_id).strip():
                                captured["json"] = jd
                except Exception:
                    pass

            # Create the detail page once, reuse it for subsequent orders
            if detail_page is None or detail_page.is_closed():
                detail_page = await page.context.new_page()

            detail_page.on("response", _intercept)

            for attempt in range(1, 3):
                try:
                    url = f"https://dealer.unifi.com.my/esales/h5/onBoarding/OrderDetails?custOrderId={order_id}&custOrderNbr={order_id}"
                    await detail_page.goto(url, wait_until="networkidle", timeout=90000)

                    for _ in range(20):
                        if "json" in captured:
                            break
                        await detail_page.wait_for_timeout(600)

                    await detail_page.wait_for_timeout(500)

                    if "json" in captured:
                        break

                    if attempt < 2:
                        print(f"  ⚠️ Attempt {attempt} failed for {order_id}, retrying...")
                        await detail_page.wait_for_timeout(3000)

                except Exception as e:
                    if attempt < 2:
                        print(f"  ⚠️ Attempt {attempt} error for {order_id}: {e}, retrying...")
                        await detail_page.wait_for_timeout(3000)
                    else:
                        print(f"  ❌ All attempts failed for {order_id}: {e}")

            detail_page.remove_listener("response", _intercept)

            if "json" not in captured:
                print(f"⚠️ No getCeeOrderDetail JSON captured for {order_id}")

            return captured.get("json", {}) or {}

        # Start scraping with crash-safe error handling
        sync_header = (
            "FULL CAPTURE - ALL ORDERS" if full_sync else "SMART INCREMENTAL SYNC"
        )
        print("\n" + "=" * 70)
        print(f"COLLECTING ORDERS ({sync_header})")
        print("=" * 70)

        total_scraped = 0
        success_count = 0
        error_count = 0
        skipped_count = 0
        updated_count = 0
        page_number = 1
        found_old_order = False  # Flag to detect when we hit old orders

        try:
            while True:
                try:
                    print(f"\n📄 Page {page_number}")

                    # Wait for table to load
                    await page.wait_for_selector("table tbody tr", timeout=35000)
                    await page.wait_for_timeout(1000)

                    # FIX: Ensure we're reading fresh DOM - force a small scroll to trigger re-render
                    await page.evaluate("window.scrollBy(0, 1)")
                    await page.evaluate("window.scrollBy(0, -1)")
                    await page.wait_for_timeout(500)

                    # Now get the order rows for THIS page only
                    # Use only the visible tbody inside .ant-table-content (prevents reading hidden clones)
                    await page.wait_for_selector(
                        "div.ant-table-content tbody.ant-table-tbody > tr.ant-table-row",
                        timeout=15000,
                    )
                    order_rows = await page.locator(
                        "div.ant-table-content tbody.ant-table-tbody > tr.ant-table-row"
                    ).all()

                    print(f"  Processing {len(order_rows)} rows...")

                    # Capture first visible row's ID to confirm pagination changes later
                    if order_rows:
                        _first_cell_text = (
                            await order_rows[0].locator("td").nth(0).text_content()
                            or ""
                        ).strip()
                        prev_first_id = (
                            _first_cell_text.split()[0]
                            if "Batch" in _first_cell_text
                            else _first_cell_text
                        )
                    else:
                        prev_first_id = ""

                    # ALSO capture the active page number before clicking Next
                    try:
                        prev_page_num = (
                            (
                                await page.locator(
                                    "li.ant-pagination-item-active"
                                ).first.text_content()
                            )
                            or ""
                        ).strip()
                    except Exception:
                        prev_page_num = ""

                    for row_idx, row in enumerate(order_rows, 1):
                        try:
                            cells = await row.locator("td").all()
                            if len(cells) < 1:
                                continue

                            # Get order ID
                            order_id_text = await cells[0].text_content()
                            if "Batch" in order_id_text:
                                order_id = order_id_text.strip().split()[0]
                            else:
                                order_id = order_id_text.strip()

                            if not (
                                order_id
                                and len(order_id) >= 10
                                and order_id[0].isdigit()
                            ):
                                continue

                            # Get UI metadata
                            event_type = (
                                (await cells[1].text_content()).strip()
                                if len(cells) > 1
                                else ""
                            )
                            order_status = (
                                (await cells[3].text_content()).strip()
                                if len(cells) > 3
                                else ""
                            )
                            raw_created = (
                                (await cells[4].text_content()).strip()
                                if len(cells) > 4
                                else ""
                            )
                            created_date = standardize_date(raw_created)

                            raw_updated = (
                                (await cells[5].text_content()).strip()
                                if len(cells) > 5
                                else ""
                            )
                            updated_date = standardize_date(raw_updated)

                            org_code = (
                                (await cells[9].text_content()).strip()
                                if len(cells) > 9
                                else ""
                            )
                            org_name = (
                                (await cells[10].text_content()).strip()
                                if len(cells) > 10
                                else ""
                            )

                            # Check if order should be skipped (applies to BOTH modes now)
                            should_skip = False

                            if order_id in complete_orders:
                                # CHECK: Is it one of the broken ones missing Org Code?
                                if order_id in orders_missing_org:
                                    should_skip = False
                                    print(
                                        f"  [{row_idx}/{len(order_rows)}] {order_id} 🛠️ (rescraping missing Org Code)"
                                    )
                                else:
                                    should_skip = True
                                    skipped_count += 1
                                    print(
                                        f"  [{row_idx}/{len(order_rows)}] {order_id} ⏭️ (up-to-date)"
                                    )
                            else:
                                # Only scrape if it is genuinely NEW
                                if order_id in seen_ids:
                                    continue
                                seen_ids.add(order_id)
                                print(
                                    f"  [{row_idx}/{len(order_rows)}] {order_id} ✨ (new)\n",
                                    end=" ",
                                )

                            if should_skip:
                                continue

                            total_scraped += 1

                            # === Frozen-ID loop + JSON fetch (proven) ===
                            # Snapshot visible IDs and their status/dates once per page;
                            # Use JSON for all other fields. No DOM scraping for Name/Email/etc.
                            if row_idx == 1:
                                try:
                                    overrides = {}
                                    visible_rows = await page.locator(
                                        "div.ant-table-content tbody.ant-table-tbody tr.ant-table-row"
                                    ).all()
                                    for vr in visible_rows:
                                        try:
                                            tds = await vr.locator("td").all()
                                            if len(tds) < 6:
                                                continue
                                            _id_text = (
                                                await tds[0].text_content()
                                            ) or ""
                                            _id = (
                                                _id_text.strip().split()[0]
                                                if "Batch" in _id_text
                                                else _id_text.strip()
                                            )
                                            if not _id:
                                                continue
                                            _event_type = (
                                                (
                                                    (await tds[1].text_content()) or ""
                                                ).strip()
                                                if len(tds) > 1
                                                else ""
                                            )
                                            _status = (
                                                (
                                                    (await tds[3].text_content()) or ""
                                                ).strip()
                                                if len(tds) > 3
                                                else ""
                                            )
                                            _created = (
                                                (
                                                    (await tds[4].text_content()) or ""
                                                ).strip()
                                                if len(tds) > 4
                                                else ""
                                            )
                                            _updated = (
                                                (
                                                    (await tds[5].text_content()) or ""
                                                ).strip()
                                                if len(tds) > 5
                                                else ""
                                            )
                                            overrides[_id] = {
                                                "Event Type": _event_type,
                                                "Order Status": _status,
                                                "Created Date": _created,
                                                "Updated Date": _updated,
                                            }
                                        except Exception:
                                            continue
                                except Exception:
                                    overrides = {}

                            api_json = await fetch_order_json_via_api(order_id)
                            # HARD GUARD: if no usable data, do NOT overwrite detail fields with blanks
                            if not isinstance(api_json, dict) or not api_json.get(
                                "data"
                            ):
                                print(
                                    f"⚠️ No API data for {order_id} – skipping detail fields"
                                )
                                # Optionally track as incomplete so you can retry later
                                try:
                                    incomplete_orders[order_id] = "NO_API_DATA"
                                except NameError:
                                    pass
                                continue

                            data = (
                                api_json.get("data", {})
                                if isinstance(api_json, dict)
                                else {}
                            )
                            installation_list = (
                                data.get("installationInfoList", []) or []
                            )
                            installation_info = (
                                installation_list[0] if installation_list else {}
                            )
                            contact_dto = (
                                installation_info.get("custContactDto", {}) or {}
                            )
                            appointment_info = installation_info.get(
                                "appointmentInfo", {}
                            )
                            cust_info = data.get("custInfo", {}) or {}

                            attr_values = data.get("attrValueList", []) or []

                            def get_attr(code: str) -> str:
                                for item in attr_values:
                                    if item.get("attrCode") == code:
                                        return item.get("value") or ""
                                return ""

                            # --- MOVED UP: Package Logic (Needed for Company Name check) ---
                            order_items = data.get("orderItemList", []) or []
                            package = ""
                            broadband_package = ""

                            # 1. Prioritize Main Offer Type "B" (Bundle)
                            for item in order_items:
                                if item.get("mainOfferType") == "B":
                                    broadband_package = (
                                        item.get("mainOfferName")
                                        or item.get("offerName")
                                        or ""
                                    )
                                    if broadband_package:
                                        package = broadband_package
                                        break

                            # 2. Fallback to the first main offer name if no Bundle is found
                            if not package:
                                for item in order_items:
                                    offer_name = (
                                        item.get("mainOfferName")
                                        or item.get("offerName")
                                        or ""
                                    )
                                    if offer_name:
                                        package = offer_name
                                        break

                            # --- Company Name Logic ---
                            company_name = ""
                            # Capture company name if package contains "biz" OR cert type is business
                            cert_type_name = cust_info.get("certTypeName", "").lower()
                            is_business = (
                                "biz" in package.lower()
                                or "business" in cert_type_name
                                or "company" in cert_type_name
                                or cust_info.get("custType") == "B"
                            )
                            if is_business:
                                company_name = cust_info.get("custName", "")

                            # --- Device Name Logic ---
                            device_name = ""
                            if "device" in package.lower():
                                for item in order_items:
                                    for offer in item.get("offerInstList", []):
                                        attrs = {a.get("attrCode"): a.get("value") for a in offer.get("attrValueList", [])}
                                        catg = attrs.get("TM_ADDITIONAL_OFFER_CATG", "")
                                        offer_name_lower = (offer.get("offerName") or "").lower()
                                        is_device = (
                                            catg == "SMART_DEVICE"
                                            or "EXP_DEVICE_ESN" in attrs
                                            or (
                                                "EXP_GOODS_DELIVERY_METHOD" in attrs
                                                and catg not in ("COMBOX", "")
                                            )
                                            or (
                                                "EXP_GOODS_DELIVERY_METHOD" in attrs
                                                and any(kw in offer_name_lower for kw in ["ipad", "tablet", "phone", "watch", "galaxy", "iphone", "samsung", "device", "premium value"])
                                            )
                                        )
                                        if is_device and offer.get("offerName"):
                                            device_name = offer.get("offerName", "")
                                            break
                                    if device_name:
                                        break

                            # Name: prefer installation contact name, fall back to customer name
                            name = (
                                contact_dto.get("contactName")
                                or cust_info.get("custName")
                                or ""
                            )

                            # Email: prefer installation contact email, fall back to attrValueList
                            email = (
                                contact_dto.get("email")
                                or get_attr("EXP_ORDER_CONTACT_EMAIL")
                                or ""
                            )

                            # Phone: combine contactDto phones + EXP_ORDER_CONTACT_NUMBER
                            phones: list[str] = []

                            for k in ("contactNbr", "mobilePhone", "homePhone"):
                                v = contact_dto.get(k)
                                if v:
                                    phones.append(str(v))

                            order_contact_phone = get_attr("EXP_ORDER_CONTACT_NUMBER")
                            if (
                                order_contact_phone
                                and order_contact_phone not in phones
                            ):
                                phones.append(order_contact_phone)

                            phone_number = ", ".join(phones)

                            # Address
                            address = installation_info.get("displayAddress") or ""

                            if not address:
                                # Fallbacks from custInfo
                                address = (
                                    cust_info.get("fullAddress")
                                    or cust_info.get("address")
                                    or ""
                                )

                            # Appointment
                            appt_start = appointment_info.get(
                                "appointmentStartTime", ""
                            )
                            appt_end = appointment_info.get("appointmentEndTime", "")
                            if appt_start and appt_end:
                                appointment_date = f"{format_datetime(appt_start)} - {format_datetime(appt_end)}"
                            elif appt_start:
                                appointment_date = format_datetime(appt_start)
                            else:
                                appointment_date = ""

                            # Prefer values from custInfo, fall back to partyCertList if needed
                            cert_number = (
                                cust_info.get("icNbr")
                                or cust_info.get("certNbr")
                                or next(
                                    (
                                        c.get("certNbr")
                                        for c in cust_info.get("partyCertList", [])
                                        if c.get("certNbr")
                                    ),
                                    "",
                                )
                                or ""
                            )

                            cert_type_name = (
                                cust_info.get("certTypeName")
                                or next(
                                    (
                                        c.get("certTypeName")
                                        for c in cust_info.get("partyCertList", [])
                                        if c.get("certTypeName")
                                    ),
                                    "",
                                )
                                or ""
                            )

                            if cert_number and cert_type_name:
                                ic_number = f"{cert_number} ({cert_type_name})"
                            else:
                                ic_number = cert_number or ""
                            party_name = data.get("partyName", "") or ""
                            party_code = data.get("partyStaffCode", "") or ""
                            creator = (
                                f"{party_name} ({party_code})"
                                if party_code
                                else party_name
                            )

                            row_data = {
                                "Order Number": order_id,
                                "Event Type": event_type,
                                "Order Status": order_status,
                                "Created Date": created_date,
                                "Updated Date": updated_date,
                                "Org Code": org_code,
                                "Organization Name": org_name,
                                "Name": name,
                                "Company Name": company_name,  # <--- NEW FIELD
                                "Email": email,
                                "Phone Number": phone_number,
                                "Appointment Date": appointment_date,
                                "Address": address,
                                "Package": package,
                                "Device": device_name,
                                "IC Number": ic_number,
                                "Creator": creator,
                                "Last Synced": "'"
                                + datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                                "Cust ID": str(cust_info.get("custId", "")),
                            }

                            if output_format == "sheets":
                                row_data["Order Number"] = f"'{order_id}"

                                # CRASH-SAFE: Save immediately after each successful scrape
                                writers.upsert_order(ws, row_data)

                                # Update our tracking
                                complete_orders[order_id] = datetime.now(LOCAL_TZ)
                                if order_id in incomplete_orders:
                                    del incomplete_orders[order_id]

                                print("✅ (saved immediately)")

                            else:
                                # CSV: append immediately
                                all_orders.append(row_data)

                                import csv as csv_lib

                                file_exists = os.path.exists(csv_path)
                                with open(
                                    csv_path, "a", newline="", encoding="utf-8"
                                ) as f:
                                    writer = csv_lib.DictWriter(
                                        f, fieldnames=list(row_data.keys())
                                    )
                                    if not file_exists:
                                        writer.writeheader()
                                    writer.writerow(row_data)

                                # Update checkpoint with datetime
                                complete_orders[order_id] = datetime.now(LOCAL_TZ)
                                if order_id in incomplete_orders:
                                    del incomplete_orders[order_id]

                                checkpoint_data = {
                                    "completed": {
                                        k: v.isoformat()
                                        for k, v in complete_orders.items()
                                    },
                                    "incomplete": incomplete_orders,
                                    "last_update": datetime.now(LOCAL_TZ).isoformat(),
                                }
                                with open(checkpoint_file, "w") as f:
                                    json.dump(checkpoint_data, f)

                                print("✅ (saved immediately)")

                            success_count += 1
                            if order_id in captured_details:
                                del captured_details[order_id]

                            # Close modal
                            try:
                                await page.click("button.ant-modal-close", timeout=2000)
                                await page.wait_for_timeout(300)
                            except:
                                try:
                                    await page.keyboard.press("Escape")
                                    await page.wait_for_timeout(300)
                                except:
                                    pass

                        except Exception as e:
                            print(f"❌ {str(e)[:40]}")
                            error_count += 1
                            try:
                                await page.keyboard.press("Escape")
                            except:
                                pass

                    # Early exit logic (only in incremental mode)
                    if not full_sync and found_old_order and page_number > 1:
                        recent_skip_ratio = skipped_count / max(
                            1, (total_scraped + skipped_count)
                        )
                        if (
                            recent_skip_ratio > 0.8
                        ):  # If >80% of recent orders are being skipped
                            print(
                                f"\n  ⏰ High skip ratio ({recent_skip_ratio:.1%}) - likely reached old data, stopping early"
                            )
                            break

                            # Next page

                    try:
                        # 1) Read current active page number from UI (source of truth)
                        try:
                            active_el = page.locator(
                                "li.ant-pagination-item-active"
                            ).first
                            active_text = (await active_el.text_content() or "").strip()
                            prev_page_num = int(active_text)
                        except Exception:
                            # Fallback: use our own counter if parsing fails
                            prev_page_num = page_number

                        target_page = prev_page_num + 1

                        # 2) Click Next (use the button inside the li to avoid clicking a disabled wrapper)
                        next_button_li = page.locator("li.ant-pagination-next").first
                        is_disabled = await next_button_li.get_attribute(
                            "aria-disabled"
                        )
                        if is_disabled == "true":
                            print(f"\n  ✅ Reached last page")
                            break

                        # Prefer the inner button when present
                        next_button = (
                            next_button_li.locator("button").first
                            if await next_button_li.locator("button").count() > 0
                            else next_button_li
                        )

                        # NEW: wait for spinner overlay to be gone before trying to click
                        try:
                            await page.wait_for_selector(
                                "div.ant-spin.ant-spin-spinning.ant-table-with-pagination.ant-table-spin-holder",
                                state="detached",
                                timeout=15000,
                            )
                        except Exception:
                            pass  # no spinner or too fast; we'll still try

                        # Click Next; if page/context already closed, bail out cleanly
                        try:
                            await next_button.click()
                        except Exception as e:
                            print(f"⚠️ Failed to click Next: {e}")
                            break

                        # 3) Wait for the spinner (if any) to appear then disappear
                        #    <span class="ant-spin-dot ant-spin-dot-spin">...</span>
                        try:
                            await page.wait_for_selector(
                                "span.ant-spin-dot-spin", timeout=3000
                            )
                        except Exception:
                            # Spinner might be too fast or not shown; ignore
                            pass

                        try:
                            await page.wait_for_selector(
                                "span.ant-spin-dot-spin",
                                state="detached",
                                timeout=25000,
                            )
                        except Exception:
                            # If it never attached or stays, we'll still rely on the next step
                            pass

                        # 4) Wait until the ACTIVE page number equals target_page
                        try:
                            await page.wait_for_function(
                                """
                                (target) => {
                                    const active = document.querySelector('li.ant-pagination-item-active');
                                    if (!active) return false;
                                    const text = (active.textContent || '').trim();
                                    return text === String(target);
                                }
                                """,
                                target_page,
                                timeout=15000,
                            )
                        except Exception:
                            # Tolerate failure; we'll still ensure rows exist
                            pass

                        # 5) Ensure the visible tbody exists again before reading rows
                        await page.wait_for_selector(
                            "div.ant-table-content tbody.ant-table-tbody > tr.ant-table-row",
                            timeout=10000,
                        )

                        # Clear captured details so only fresh responses from this page are considered
                        captured_details.clear()

                        # Keep our counter aligned with the UI page number
                        page_number = target_page
                    except Exception:
                        break

                except Exception as e:
                    print(f"\n  ❌ Page error: {e}")
                    break

        except Exception as e:
            print(f"\n💥 CRASH DETECTED: {e}")
            print(
                f"✅ All successfully scraped data was saved immediately to Google Sheets"
            )
            print(f"✅ {success_count} orders were saved before crash")
            # Don't re-raise the exception - let the summary run

        # No need to flush - we save immediately after each successful scrape

        # Generate summary for Telegram (counts only, no full data)
        if output_format == "sheets":
            try:
                # Count orders by status
                completed_count = 0
                cancelled_count = 0
                new_orders_count = 0
                other_count = 0
                total_in_sheet = 0

                # Re-read the sheet to count orders
                all_records = ws.get_all_values()

                # Reset counters
                total_in_sheet = 0
                new_orders_count = 0

                for row in all_records[1:]:  # Skip header
                    if not row:
                        continue

                    order_status = row[2].strip() if len(row) > 2 and row[2] else ""
                    last_synced = row[13].strip() if len(row) > 13 and row[13] else ""

                    # Only count rows that have a Last Synced value
                    if last_synced:
                        total_in_sheet += 1

                        # Check if this order was just scraped (Last Synced = today)
                        try:
                            last_synced_dt = parse_last_synced(last_synced)
                            if (
                                last_synced_dt
                                and last_synced_dt.date()
                                == datetime.now(LOCAL_TZ).date()
                            ):
                                new_orders_count += 1
                        except Exception:
                            pass

                    # Check if this order was just scraped (has Last Synced from today)
                    is_newly_scraped = False
                    if last_synced:
                        try:
                            last_synced_dt = parse_last_synced(last_synced)
                            if (
                                last_synced_dt
                                and last_synced_dt.date()
                                == datetime.now(LOCAL_TZ).date()
                            ):
                                is_newly_scraped = True
                                new_orders_count += 1
                        except:
                            pass

                    # Count by status
                    if order_status == "Completed":
                        completed_count += 1
                    elif order_status == "Cancelled":
                        cancelled_count += 1
                    else:
                        other_count += 1

                # Create summary JSON
                summary = {
                    "date": datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"),
                    "time": datetime.now(LOCAL_TZ).strftime("%H:%M:%S"),
                    "month": month_text,
                    "year": year,
                    "tab_name": tab_title,
                    "scrape_mode": "full_sync" if full_sync else "incremental",
                    "summary": {
                        "total_in_sheet": total_in_sheet,
                        "completed": completed_count,
                        "cancelled": cancelled_count,
                        "other_statuses": other_count,
                        "new_today": new_orders_count,
                    },
                    "scrape_stats": {
                        "orders_processed": total_scraped,
                        "successful": success_count,
                        "skipped": skipped_count,
                        "failed": error_count,
                    },
                }

                # Save summary to single file
                summary_dir = os.path.join(OUTPUT_DIR, "summaries")
                os.makedirs(summary_dir, exist_ok=True)

                timestamp = datetime.now(LOCAL_TZ).strftime("%Y%m%d_%H%M%S")
                summary_file = os.path.join(summary_dir, f"summary_{timestamp}.json")

                with open(summary_file, "w", encoding="utf-8") as f:
                    json.dump(summary, f, indent=2, ensure_ascii=False)

                print(f"\n📊 SUMMARY FOR TELEGRAM:")
                print(f"   Total orders in sheet: {total_in_sheet}")
                print(f"   ✅ Completed: {completed_count}")
                print(f"   ❌ Cancelled: {cancelled_count}")
                print(f"   📋 Other statuses: {other_count}")
                print(f"   ✨ New orders today: {new_orders_count}")
                print(f"   💾 Summary saved: {summary_file}")

            except Exception as e:
                print(f"⚠️ Warning: Could not generate summary: {e}")

        # Sort the tab by Created Date after scraping (sheets mode only)
        if output_format == "sheets":
            try:
                from gsheets_writer import sort_tab_by_created_date

                print(f"\n🔄 Sorting tab by Created Date...")
                sort_tab_by_created_date(ws, descending=True)
            except Exception as e:
                print(f"⚠️ Warning: Could not sort tab: {e}")

        # Run custId update + status check if enabled (sheets mode only)
        if check_status and output_format == "sheets":
            # Step 1: Update 10XXX custIds to newer ones
            try:
                from check_custid import check_custids_for_month
                from check_status import check_all_statuses, navigate_to_order_entry

                print(f"\n🔄 Updating 10XXX custIds...")
                iframe_frame = await navigate_to_order_entry(page)

                # CSRF capture
                captured_csrf = {}
                async def _cap_csrf(route):
                    csrf = route.request.headers.get("x-csrf-token", "")
                    if csrf:
                        captured_csrf["token"] = csrf
                    await route.continue_()

                await page.context.route("**/*", _cap_csrf)
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
                except Exception:
                    pass
                await page.context.unroute("**/*")
                csrf_token = captured_csrf.get("token", "")

                if csrf_token:
                    await check_custids_for_month(
                        page, iframe_frame, csrf_token, month_text, year, write=True, ws=ws
                    )
                else:
                    print("  ⚠️ CSRF not captured — skipping custId update")
            except Exception as e:
                print(f"⚠️ Warning: CustId update failed: {e}")

            # Step 2: Re-read the sheet (custIds may have changed) and check statuses
            try:
                print(f"\n🔍 Running subscriber status check...")
                # Re-open the worksheet to get fresh data after custId updates
                ws = spread.worksheet(tab_title)
                status_result = await check_all_statuses(page, month_text, year, ws, iframe_frame=iframe_frame)
                print(f"  Status check complete: {status_result.get('checked', 0)} checked, "
                      f"{status_result.get('not_found', 0)} not found, "
                      f"{status_result.get('errors', 0)} errors")
            except Exception as e:
                print(f"⚠️ Warning: Status check failed: {e}")

        # Cleanup CSV checkpoint
        if output_format == "csv":
            if os.path.exists(checkpoint_file):
                os.remove(checkpoint_file)
                print(f"\n✅ Checkpoint removed")
            print(f"💾 Final CSV: {csv_path} ({len(all_orders)} orders)")

        # Summary
        summary_title = "FULL CAPTURE SUMMARY" if full_sync else "SMART SYNC SUMMARY"
        print("\n" + "=" * 70)
        print(f"{summary_title}")
        print("=" * 70)
        print(
            f"Mode: {'Full Sync (captured all orders)' if full_sync else 'Incremental Sync (smart date comparison)'}"
        )
        print(f"Agents: {agent_count}")
        print(f"Pages: {page_number}")
        print(f"Orders processed: {total_scraped}")
        if not full_sync:
            print(f"✨ New: {total_scraped - updated_count - len(incomplete_orders)}")
            print(f"🔄 Updated: {updated_count}")
            print(f"🔄 Incomplete: {len(incomplete_orders)}")
            print(f"⏭️ Skipped (up-to-date): {skipped_count}")
        else:
            print(f"🔄 Re-scraped existing: {updated_count}")
            print(f"✨ New orders: {total_scraped - updated_count}")
        print(f"✅ Successful: {success_count}")
        print(f"❌ Failed: {error_count}")
        print(f"💾 Data Safety: Each order saved immediately to Google Sheets")
        print("=" * 70)

        result = {
            "success": True,
            "total": total_scraped,
            "successful": success_count,
            "skipped": skipped_count,
            "failed": error_count,
            "updated": updated_count,
            "agents_selected": agent_count,
            "pages_scraped": page_number,
        }

        if output_format == "sheets":
            result["sheet_tab"] = tab_title
        else:
            result["csv_file"] = csv_path
            result["orders"] = all_orders

        return result

    finally:
        # Close the reusable detail page if it was created
        try:
            dp = locals().get("detail_page")
            if dp and not dp.is_closed():
                await dp.close()
        except Exception:
            pass
        await context.close()
        await browser.close()
        await pw.stop()


# Convenience wrappers
async def scrape_to_sheets(
    username: str,
    password: str,
    month_text: str,
    year: int,
    full_sync: bool = False,
    check_status: bool = False,
):
    return await scrape_orders_month(
        username, password, month_text, year, "sheets", None, full_sync, check_status
    )


async def scrape_to_csv(
    username: str,
    password: str,
    month_text: str,
    year: int,
    csv_filename: Optional[str] = None,
    full_sync: bool = False,
):
    return await scrape_orders_month(
        username,
        password,
        month_text,
        year,
        "csv",
        csv_filename,
        full_sync,
    )


# New convenience functions for specific modes
async def scrape_full_sync_to_sheets(
    username: str, password: str, month_text: str, year: int, check_status: bool = False
):
    """Scrape ALL orders to sheets (ignores existing data)"""
    return await scrape_orders_month(
        username,
        password,
        month_text,
        year,
        "sheets",
        None,
        full_sync=True,
        check_status=check_status,
    )


async def scrape_incremental_to_sheets(
    username: str, password: str, month_text: str, year: int, check_status: bool = False
):
    """Smart incremental sync to sheets (only new/updated orders)"""
    return await scrape_orders_month(
        username,
        password,
        month_text,
        year,
        "sheets",
        None,
        full_sync=False,
        check_status=check_status,
    )


def scrape_month(month_text: str, year: int, full_sync: bool = True, check_status: bool = False):
    """
    Synchronous wrapper for API - loads credentials and runs scrape
    """
    import asyncio

    from credential_manager import CredentialManager

    # Load credentials
    cred_manager = CredentialManager()
    if not cred_manager.credentials_exist():
        return {"success": False, "error": "No credentials saved"}

    creds = cred_manager.get_credentials()
    username = creds.get("username")
    password = creds.get("password")

    # Run async scrape
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            scrape_orders_month(
                username,
                password,
                month_text,
                year,
                output_format="sheets",
                csv_filename=None,
                full_sync=full_sync,
                check_status=check_status,
            )
        )
        return result
    finally:
        loop.close()
