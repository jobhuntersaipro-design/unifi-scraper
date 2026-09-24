"""
Pure UI scraper with improved incremental sync based on Updated Date vs Last Synced
Supports both CSV export (Telegram) and Google Sheets (daily)
"""

import csv
import json
import os
import sys
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

# Infrastructure/wholesale offers that appear as their own entry in orderItemList
# but are never the customer-facing package. An order like 2608000122201886 lists
# "BitStream" (the access layer) before the real plan, so taking the first offer
# name would report the carrier product instead of what the customer bought.
# Matched on the whole normalised name, not as a substring, so a genuine plan that
# merely mentions one of these words is not discarded.
GENERIC_OFFER_NAMES = {"bitstream", "broadband bundle"}


def _is_generic_offer(name: str) -> bool:
    """True if `name` is a placeholder/infrastructure offer, not a real package."""
    return (name or "").strip().lower() in GENERIC_OFFER_NAMES


def _offer_name(item: dict) -> str:
    return item.get("mainOfferName") or item.get("offerName") or ""


# The Retail Order page is one URL (/esales/retailHistory) with two pills that
# swap the table in place without navigating, so a tab is chosen by clicking its
# label. Everything after the click -- agents, month picker, pagination, Details
# -- is identical between them.
TABS = {"history": "History", "ongoing": "Ongoing"}

# History first: an order that completes between the two passes should end up
# recorded as completed, not left at the in-flight state an Ongoing row would
# write over it.
DEFAULT_TABS = ("history", "ongoing")

# Order states worth scraping per tab; None means take everything.
#
# Ongoing lists Provisioning, Waiting for payment and On-held. Only
# Provisioning is wanted -- the rest are not committed work yet. Nothing is
# lost by skipping them: when one does progress it lands in History, and
# gsheets_writer.upsert_rows keys on Order Number, so it updates that order's
# existing row rather than adding a second one.
#
# Matched as a substring because the cell renders with a status dot, so the
# text can arrive as "• Provisioning".
TAB_STATE_FILTER = {"ongoing": ("provisioning",), "history": None}

# Order states that will never change again. Anything else -- Provisioning,
# Waiting for payment, On-held, or blank -- is still in flight and must be
# re-checked on later runs even though the row already carries a Last Synced.
#
# Without this, incremental sync skips any order that has ever been synced, so
# a row first seen mid-flight keeps that state forever: 2607000117240493 and
# 2607000118620451 sat at "Provisioning" from 18 Aug because every nightly run
# skipped them. Deliberately a short allowlist -- an unknown state re-checks,
# which costs a fetch, while wrongly calling something terminal loses the
# update entirely.
TERMINAL_ORDER_STATES = ("completed", "cancelled", "canceled", "void")


def _is_terminal_state(state: str) -> bool:
    s = (state or "").strip().lower()
    return any(t in s for t in TERMINAL_ORDER_STATES)

# Cell positions used when the header row cannot be read. These are the
# long-standing History positions.
FALLBACK_COLS = {
    "order": 0,
    "event": 1,
    "state": 3,
    "created": 4,
    "updated": 5,
    "org_code": 9,
    "org_name": 10,
}

# Header label -> logical key. Several spellings map to one key because the two
# tabs do not label these columns identically ("Last Updated Date" on Ongoing).
HEADER_ALIASES = {
    "order number": "order",
    "event type": "event",
    "order state": "state",
    "order status": "state",
    "created date": "created",
    "last updated date": "updated",
    "updated date": "updated",
    "org code": "org_code",
    "organization name": "org_name",
    "organisation name": "org_name",
}


async def column_index_map(page) -> Dict[str, int]:
    """Map logical column keys to <td> positions by reading the table header.

    Positions were previously hardcoded, which is safe only while both tabs
    share a layout. Ongoing carries a "Last Updated Date" column, and whatever
    sits at positions 9 and 10 is off-screen behind a horizontal scroll on both
    tabs -- so a shifted column would silently file Org Code under Organization
    Name rather than fail. Reading the header makes that impossible; the fixed
    positions remain as a fallback so a missing header row cannot stop a scrape.
    """
    cols = dict(FALLBACK_COLS)
    try:
        headers = await page.locator(
            "div.ant-table-content thead.ant-table-thead th"
        ).all_text_contents()
        found = {}
        for i, label in enumerate(headers):
            key = HEADER_ALIASES.get(label.strip().lower())
            if key and key not in found:
                found[key] = i
        if not found:
            print("  ⚠️ No recognisable table headers — using fixed column positions")
            return cols
        cols.update(found)
        missing = sorted(set(FALLBACK_COLS) - set(found))
        if missing:
            print(f"  ⚠️ Headers missing {missing} — fixed positions used for those")
        moved = {k: (FALLBACK_COLS[k], v) for k, v in found.items()
                 if FALLBACK_COLS[k] != v}
        if moved:
            print(f"  ℹ️ Column positions differ from default: {moved}")
    except Exception as e:
        print(f"  ⚠️ Could not read table headers ({e}) — using fixed positions")
    return cols



def tabs_from_argv(argv=None) -> Tuple[str, ...]:
    """Which tabs a runner script should scrape, from its command line.

        python run_scrape_test_sep.py             -> both
        python run_scrape_test_sep.py --history   -> History only
        python run_scrape_test_sep.py --ongoing   -> Ongoing only

    Passing both flags means both, which is already the default.
    """
    argv = sys.argv[1:] if argv is None else argv
    history, ongoing = "--history" in argv, "--ongoing" in argv
    if history and not ongoing:
        return ("history",)
    if ongoing and not history:
        return ("ongoing",)
    return DEFAULT_TABS


def _cell_text(cells, cols, key):
    """Text of the column `key`, or '' when the row is short or key unmapped."""
    i = cols.get(key, -1)
    return cells[i] if 0 <= i < len(cells) else ""


def _is_uni5g(name: str) -> bool:
    return (name or "").strip().lower().startswith("uni5g")


def select_service_numbers(order_items: list, package: str = "") -> str:
    """Mobile service numbers for a UNI5G order, as `<number>(<iccid>)`.

    The portal splits an MSISDN across two fields -- `prefix` holds the country
    code ("60") and `accNbr` the rest -- so the full number exists nowhere in
    the response and has to be joined. `iccid` (the SIM serial) sits directly on
    the order item, not down in offerInstList.attrValueList where the fibre
    orders keep their device serial.

    One order carries one line per item, so this returns a comma-separated list
    in source order. Do not sort it: item 2 of order 2609000125646283 pairs
    ...6983 with ...404 while item 3 pairs ...7291 with ...388, so sorting would
    silently mismatch numbers to SIMs.

    Empty for anything that is not UNI5G -- fibre items carry non-numeric
    accNbrs like "HSTB11382107" and "avionic5043@unifi", which are not service
    numbers.
    """
    if not _is_uni5g(package):
        return ""

    numbers = []
    for item in order_items:
        # A mobile line, identified either by its own UNI5G offer or by having
        # a SIM. Keeps a fibre item out of a mixed bundle.
        if not (_is_uni5g(_offer_name(item)) or item.get("iccid")):
            continue
        acc = str(item.get("accNbr") or "").strip()
        if not acc:
            continue
        number = f"{str(item.get('prefix') or '').strip()}{acc}"
        iccid = str(item.get("iccid") or "").strip()
        numbers.append(f"{number}({iccid})" if iccid else number)

    return ",".join(numbers)


def select_package(order_items: list) -> str:
    """Pick the customer-facing package name out of an order's orderItemList.

    Preference order:
      1. An explicit bundle (mainOfferType "B").
      2. The first offer that is not infrastructure (see GENERIC_OFFER_NAMES).
      3. The first offer of any kind — only if every entry was generic, so the
         row still carries something traceable instead of an empty package.
    """
    # 1. Prioritize Main Offer Type "B" (Bundle)
    for item in order_items:
        if item.get("mainOfferType") == "B":
            name = _offer_name(item)
            if name and not _is_generic_offer(name):
                return name

    # 2. First real offer, skipping infrastructure entries like BitStream so the
    #    customer-facing plan further down the list wins.
    for item in order_items:
        name = _offer_name(item)
        if name and not _is_generic_offer(name):
            return name

    # 3. Last resort: everything was generic.
    for item in order_items:
        name = _offer_name(item)
        if name:
            return name

    return ""


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

    # Open channel modal. The trigger is an anchor wrapping an icon-font glyph:
    #   <a title="Subordinate"><span class="iconfont icon-chooseChannel"></span></a>
    # The old img[src*=...] / img[alt*=...] pair could never match either element, and
    # the fallback's timeout propagated straight out of the except, killing the run.
    # Click the <a> (which carries the handler), not the zero-size span. Resolve it by
    # title first, then by walking up from the span — the portal localises titles on
    # some loads, so "Subordinate" alone is not dependable.
    print("  🖼️ Opening channel selection modal...")
    try:
        clicked = await page.evaluate(
            """() => {
                const span = document.querySelector('span.icon-chooseChannel');
                const el = document.querySelector('a[title="Subordinate"]')
                    || (span && span.closest('a'))
                    || span;
                if (!el) return false;
                el.click();
                return true;
            }"""
        )
        if not clicked:
            raise RuntimeError(
                'neither a[title="Subordinate"] nor span.icon-chooseChannel is in the DOM'
            )
        await page.wait_for_timeout(2000)
    except Exception as e:
        print(f"  ⚠️ DOM click on channel icon failed ({e}); trying forced click...")
        await page.click(
            'a[title="Subordinate"], span.icon-chooseChannel', force=True, timeout=5000
        )
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
) -> Tuple[Dict[str, datetime], Dict[str, int], set, set]:
    """
    Check Google Sheet for existing orders, their dates, and MISSING DATA.
    Returns:
        - complete_orders: {order_id: last_synced_datetime}
        - incomplete_orders: {order_id: row_index}
        - orders_missing_org: set(order_ids) -> IDs that have Last Synced but no Org Code
        - orders_unsettled: set(order_ids) -> synced, but still in a non-terminal
          state, so their stored status is stale and must be re-fetched
    """
    complete_orders = {}
    incomplete_orders = {}
    orders_missing_org = set()
    orders_unsettled = set()

    try:
        records = ws.get_all_values()
        if not records:
            return {}, {}, set(), set()

        headers = records[0]
        print(f"  📊 Checking {len(records)-1} existing orders...")

        # Dynamically find "Org Code" column index (safer than hardcoding)
        try:
            org_code_idx = headers.index("Org Code")
        except ValueError:
            org_code_idx = -1  # Column not found

        try:
            status_idx = headers.index("Order Status")
        except ValueError:
            status_idx = -1

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

                # Synced while still in flight -- the stored status is a
                # snapshot, not a conclusion, so it has to be looked at again.
                if status_idx != -1:
                    stored = row[status_idx] if len(row) > status_idx else ""
                    if not _is_terminal_state(stored):
                        orders_unsettled.add(order_number)
            else:
                incomplete_orders[order_number] = idx

        return (
            complete_orders,
            incomplete_orders,
            orders_missing_org,
            orders_unsettled,
        )

    except Exception as e:
        print(f"⚠️ Error checking orders: {e}")
        return {}, {}, set(), set()


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
    tab: str = "history",  # "history" (completed) or "ongoing" (in-flight)
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

        # Select the Ongoing/History pill. Same URL either way — the table is
        # swapped in place, so this click is the only thing that differs.
        tab_label = TABS[tab]
        print(f"\n📑 Selecting {tab_label} tab...")
        print(f"  📍 Current URL: {page.url}")
        await page.screenshot(path=f"logs/before_{tab}_click.png")

        # Retry — the page may still be loading
        tab_clicked = False
        for attempt in range(3):
            try:
                await page.locator(f'text="{tab_label}"').last.click(timeout=15000)
                tab_clicked = True
                print(f"  ✅ {tab_label} tab clicked (attempt {attempt + 1})")
                await page.wait_for_timeout(10000)
                break
            except Exception as e:
                print(f"  ⚠️ Attempt {attempt + 1} failed: {e}")
                await page.wait_for_timeout(5000)

        if not tab_clicked:
            print(f"  ❌ All {tab_label} tab attempts failed")
            await page.screenshot(path=f"logs/{tab}_tab_failed.png")

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
            (
                complete_orders,
                incomplete_orders,
                orders_missing_org,
                orders_unsettled,
            ) = await check_existing_orders_with_dates(ws)

            if complete_orders:
                print(f"  ✅ {len(complete_orders)} orders with sync dates")
            if orders_missing_org:
                print(
                    f"  ⚠️ {len(orders_missing_org)} orders missing Org Code (will rescrape)"
                )
            if orders_unsettled:
                print(
                    f"  ⏳ {len(orders_unsettled)} orders still in a non-terminal "
                    f"state (will rescrape to refresh status)"
                )
            if incomplete_orders:
                print(f"  🔄 {len(incomplete_orders)} incomplete orders")
        else:
            # CSV mode: Use checkpoint
            complete_orders = {}
            incomplete_orders = {}
            # Both are only populated from a sheet, but the skip check below
            # reads them on every path -- orders_missing_org was previously
            # left undefined here, which would raise NameError on the first
            # already-synced order in a CSV run.
            orders_missing_org = set()
            orders_unsettled = set()

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

                    # Resolve column positions from the header once per page —
                    # Ongoing and History are not guaranteed to share a layout.
                    cols = await column_index_map(page)

                    # States this tab cares about (None = all). Ongoing is
                    # narrowed to Provisioning; see TAB_STATE_FILTER.
                    wanted_states = TAB_STATE_FILTER.get(tab)
                    state_skipped = 0

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
                            cell_els = await row.locator("td").all()
                            if len(cell_els) < 1:
                                continue

                            # One pass over the row; positions come from the
                            # header map rather than being hardcoded.
                            cells = [
                                ((await c.text_content()) or "").strip()
                                for c in cell_els
                            ]

                            # Get order ID
                            order_id_text = _cell_text(cells, cols, "order")
                            if "Batch" in order_id_text:
                                order_id = order_id_text.split()[0]
                            else:
                                order_id = order_id_text

                            if not (
                                order_id
                                and len(order_id) >= 10
                                and order_id[0].isdigit()
                            ):
                                continue

                            # Get UI metadata
                            event_type = _cell_text(cells, cols, "event")
                            order_status = _cell_text(cells, cols, "state")

                            # Skip states this tab does not want, before the
                            # expensive part: each kept row opens its detail
                            # page and waits on an API response.
                            if wanted_states and not any(
                                w in order_status.lower() for w in wanted_states
                            ):
                                state_skipped += 1
                                continue

                            created_date = standardize_date(
                                _cell_text(cells, cols, "created")
                            )
                            updated_date = standardize_date(
                                _cell_text(cells, cols, "updated")
                            )
                            org_code = _cell_text(cells, cols, "org_code")
                            org_name = _cell_text(cells, cols, "org_name")

                            # Check if order should be skipped (applies to BOTH modes now)
                            should_skip = False

                            if order_id in complete_orders:
                                # An order still in flight must be refreshed:
                                # its stored status is a snapshot, not a result.
                                if order_id in orders_unsettled:
                                    should_skip = False
                                    print(
                                        f"  [{row_idx}/{len(order_rows)}] {order_id} "
                                        f"⏳ (refreshing non-terminal status)"
                                    )
                                # CHECK: Is it one of the broken ones missing Org Code?
                                elif order_id in orders_missing_org:
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
                            package = select_package(order_items)
                            service_numbers = select_service_numbers(
                                order_items, package
                            )

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
                                "Service Number": service_numbers,
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

                    if state_skipped:
                        kept = len(order_rows) - state_skipped
                        print(
                            f"  🔎 {TABS[tab]}: kept {kept}/{len(order_rows)} rows "
                            f"({state_skipped} skipped — state not in "
                            f"{list(wanted_states)})"
                        )

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
    tabs: Tuple[str, ...] = DEFAULT_TABS,
):
    return await scrape_tabs_to_sheets(
        username, password, month_text, year, full_sync, check_status, tabs
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
    username: str,
    password: str,
    month_text: str,
    year: int,
    check_status: bool = False,
    tabs: Tuple[str, ...] = DEFAULT_TABS,
):
    """Scrape ALL orders to sheets (ignores existing data), both tabs."""
    return await scrape_tabs_to_sheets(
        username,
        password,
        month_text,
        year,
        full_sync=True,
        check_status=check_status,
        tabs=tabs,
    )


async def scrape_tabs_to_sheets(
    username: str,
    password: str,
    month_text: str,
    year: int,
    full_sync: bool = False,
    check_status: bool = False,
    tabs: Tuple[str, ...] = DEFAULT_TABS,
) -> Dict:
    """Scrape each tab in turn into the same monthly sheet.

    Sequential, not parallel: both write the same worksheet, and the second
    pass reuses the session cache the first one saved -- so only the first can
    cost an OTP. A browser per tab also keeps peak memory down, which matters
    on a 1GB host.

    History is done first so that an order which completed between the two
    passes is written as completed rather than being left at its in-flight
    state by a later Ongoing row.

    check_status runs only after the final tab: it walks the whole sheet, so
    running it per tab would repeat the same work.
    """
    per_tab, last = {}, len(tabs) - 1
    for i, name in enumerate(tabs):
        if name not in TABS:
            raise ValueError(f"unknown tab {name!r}; expected one of {sorted(TABS)}")
        print(f"\n{'#' * 70}")
        print(f"# {TABS[name].upper()} — {month_text} {year}  ({i + 1}/{len(tabs)})")
        print(f"{'#' * 70}")
        per_tab[name] = await scrape_orders_month(
            username,
            password,
            month_text,
            year,
            "sheets",
            None,
            full_sync=full_sync,
            check_status=check_status and i == last,
            tab=name,
        )

    totals = {k: 0 for k in ("total", "successful", "skipped", "failed", "updated")}
    for r in per_tab.values():
        for k in totals:
            totals[k] += r.get(k, 0) or 0

    print(f"\n{'=' * 70}")
    print(f"ALL TABS COMPLETE — {month_text} {year}")
    for name, r in per_tab.items():
        print(f"  {TABS[name]:<8} total={r.get('total', 0)} "
              f"ok={r.get('successful', 0)} failed={r.get('failed', 0)}")
    print(f"{'=' * 70}")

    return {
        "success": all(r.get("success") for r in per_tab.values()),
        "tabs": per_tab,
        **totals,
    }


async def scrape_incremental_to_sheets(
    username: str,
    password: str,
    month_text: str,
    year: int,
    check_status: bool = False,
    tabs: Tuple[str, ...] = DEFAULT_TABS,
):
    """Smart incremental sync to sheets (only new/updated orders), both tabs."""
    return await scrape_tabs_to_sheets(
        username,
        password,
        month_text,
        year,
        full_sync=False,
        check_status=check_status,
        tabs=tabs,
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

