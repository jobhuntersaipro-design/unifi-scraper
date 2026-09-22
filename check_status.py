"""
check_status.py - Check subscriber status via API (QryCustInfoByParamsEx)
Navigates to Order Entry once to capture session context, then queries
each order via direct fetch() calls — no UI clicking per query.
Writes Status + Status Updated Time back to Google Sheet.
"""

import re
import time
from datetime import datetime
from typing import Dict, List, Tuple

from playwright.async_api import Page

from gsheets_writer import month_tab_title, open_sheet
import neon_writer
from neon_writer import StatusUpdate

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

LOCAL_TZ = ZoneInfo("Asia/Kuala_Lumpur") if ZoneInfo else None

ORDER_ENTRY_URL = "https://dealer.unifi.com.my/esales/crm-TYMH100163"
IFRAME_SELECTOR = "#myIframe"  # fixed id of the FishModule order-entry iframe
API_URL = "https://dealer.unifi.com.my/esales/FishModule/cvbs/callservice.json?service=CallOcsDubboService&serviceName=QryCustInfoByParamsEx"

# Map cert type names (from the sheet IC Number field) to certTypeId values
CERT_TYPE_ID_MAP = {
    "MyKad": "1",
    "Passport": "2",
    "Police/Army": "3",
    "Police ID": "3",
    "Old IC": "4",
    "Birth Cert": "5",
    "Channel Account": "6",
    "Business Reg": "8",
    "Company with Business Registration Number": "8",
    "Company without Business Registration Number": "9",
    "I-KAD": "10",
    "Internal Division ID": "11",
    "MyKAS": "12",
    "MyTentera": "2",
    "MyPR": "14",
    "Prospect Customer": "7",
    "Without NRIC": "15",
    "Others": "99",
}

# prodStateName mapping for cleaner output
PROD_STATE_MAP = {
    "A": "Active",
    "S": "Suspended",
    "T": "Terminated",
    "P": "Pre-active",
}


def parse_ic_number(ic_field: str) -> Tuple[str, str]:
    """
    Parse IC Number field like '960330015853 (MyKad)' into (number, type).
    Returns (cert_number, cert_type_name) or ('', '') if unparsable.
    """
    if not ic_field or not ic_field.strip():
        return "", ""

    ic_field = ic_field.strip()

    # Match the LAST (...) group as the cert type
    # Handles cases like "MBPJ.COB.04108.2025(6) (Company with Business Registration Number)"
    match = re.match(r"^(.+)\s+\(([^)]+)\)\s*$", ic_field)
    if match:
        return match.group(1).strip(), match.group(2).strip()

    clean = ic_field.strip()
    if clean:
        return clean, ""

    return "", ""


def get_orders_to_check(ws, only_empty: bool = False) -> Tuple[List[Dict], List[Tuple[int, str]]]:
    """
    Read all orders from the worksheet that need status checking.
    Uses custId for direct qrySubsPageTree queries (no Advanced Query needed).
    If only_empty=True, only returns orders without a Status value.
    """
    orders = []
    cancelled_rows = []
    try:
        records = ws.get_all_values()
        if not records or len(records) <= 1:
            return orders, cancelled_rows

        headers = records[0]

        def col_idx(name):
            try:
                return headers.index(name)
            except ValueError:
                return -1

        idx_order = col_idx("Order Number")
        idx_order_status = col_idx("Order Status")
        idx_cust_id = col_idx("Cust ID")
        idx_status = col_idx("Status")
        idx_address = col_idx("Address")
        idx_package = col_idx("Package")
        idx_ic = col_idx("IC Number")
        idx_name = col_idx("Name")

        if idx_order == -1:
            print("  Missing Order Number column")
            return orders, cancelled_rows

        skipped_no_cust_id = []
        for row_num, row in enumerate(records[1:], start=2):
            if not row or len(row) <= idx_order:
                continue

            order_number = row[idx_order].strip().lstrip("'")
            if not order_number:
                continue

            # Pad row
            max_idx = max(idx_order, idx_order_status or 0, idx_cust_id or 0, idx_status or 0,
                          idx_address if idx_address != -1 else 0, idx_package if idx_package != -1 else 0,
                          idx_ic if idx_ic != -1 else 0, idx_name if idx_name != -1 else 0)
            while len(row) <= max_idx:
                row.append("")

            # Collect cancelled orders to fill with "-" but skip querying
            if idx_order_status != -1:
                if row[idx_order_status].strip().lower() == "cancelled":
                    if idx_status != -1 and not row[idx_status].strip():
                        # Carries the order number so the "-" write can
                        # be mirrored to Neon, which is keyed by order
                        # number rather than by sheet row.
                        cancelled_rows.append((row_num, order_number))
                    continue

            # Skip orders that already have a Status if only_empty mode
            if only_empty and idx_status != -1:
                if row[idx_status].strip():
                    continue

            # Get custId
            cust_id = row[idx_cust_id].strip() if idx_cust_id != -1 else ""

            if not cust_id:
                skipped_no_cust_id.append(order_number)
                continue

            address = row[idx_address].strip() if idx_address != -1 else ""
            package = row[idx_package].strip() if idx_package != -1 else ""
            ic_number = row[idx_ic].strip() if idx_ic != -1 else ""
            name = row[idx_name].strip() if idx_name != -1 else ""

            orders.append({
                "row_index": row_num,
                "order_number": order_number,
                "cust_id": cust_id,
                "address": address,
                "package": package,
                "ic_number": ic_number,
                "name": name,
            })

        if skipped_no_cust_id:
            print(f"  Skipped {len(skipped_no_cust_id)} orders with no Cust ID: {skipped_no_cust_id[:10]}{'...' if len(skipped_no_cust_id) > 10 else ''}")
        if cancelled_rows:
            print(f"  {len(cancelled_rows)} cancelled orders to mark with '-'")

        return orders, cancelled_rows

    except Exception as e:
        print(f"  Error reading orders: {e}")
        return orders, cancelled_rows


async def navigate_to_order_entry(page: Page):
    """
    Navigate to Order Entry and open the Advanced Query form.
    Returns the iframe frame where the form lives.
    """
    await page.goto(ORDER_ENTRY_URL, wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(3000)

    if "no-devtool" in page.url.lower() or "login" in page.url.lower():
        print(f"  ⚠️ Landed on {page.url} — session invalid or anti-bot detector fired")

    # Clear the 'Later'/announcement modal on the outer Ant shell FIRST — if left up
    # it blocks the CCEntryView app inside the iframe from initializing. (bizzflow pattern)
    try:
        modal = page.locator(".ant-modal-wrap")
        if await modal.count() > 0 and await modal.first.is_visible():
            for sel in (
                'button.ant-btn:has-text("Later")',
                ".ant-modal-close",
                'button.ant-btn:has-text("Cancel")',
                'button.ant-btn:has-text("Close")',
            ):
                btn = page.locator(sel)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click()
                    await page.wait_for_timeout(800)
                    print("  ✅ Cleared announcement modal")
                    break
    except Exception:
        pass

    # Get the order iframe by its fixed id (#myIframe) as a Frame object (needed for
    # the API path's .evaluate()/fetch). The element appears well before its app renders.
    frame = page
    try:
        await page.wait_for_selector(IFRAME_SELECTOR, timeout=15000)
        iframe_el = await page.query_selector(IFRAME_SELECTOR)
        content = await iframe_el.content_frame() if iframe_el else None
        if content:
            frame = content
    except Exception:
        print(f"  ⚠️ {IFRAME_SELECTOR} never appeared; scanning frames by URL")

    # Fallback: locate the FishModule frame by URL if #myIframe wasn't found.
    if frame == page:
        for f in page.frames:
            if f == page.main_frame:
                continue
            if "remote.html" in f.url or "orderentry" in f.url.lower() or "FishModule" in f.url:
                frame = f
                break

    # The CCEntryView app is slow + AJAX-heavy (~10s) and the iframe element appears
    # long before its app renders. Wait up to 45s for the Advanced Query button — the
    # real "app rendered" anchor the status flow needs. (bizzflow pattern)
    app_ready = False
    try:
        await frame.wait_for_selector("div.js-advanced-query-btn", state="attached", timeout=45000)
        app_ready = True
        print("  ✅ Order Entry app rendered (Advanced Query button present)")
    except Exception:
        print("  >> Advanced Query button not found after 45s (API path will still run)")

    # Open Advanced Query (UI-form path)
    if app_ready:
        try:
            await frame.locator("div.js-advanced-query-btn").first.click(timeout=8000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            print(f"  (Advanced Query open failed: {e})")

    return frame


REFERER_URL = "https://dealer.unifi.com.my/esales/FishModule/remote.html?crm/modules/pos/orderentry/views/CCEntryView"


def build_api_payload(
    cert_number: str,
    cert_type: str,
    cust_name: str,
) -> Dict:
    """
    Build the API payload for QryCustInfoByParamsEx.
    """
    cert_type_id = CERT_TYPE_ID_MAP.get(cert_type, "1")

    data = {
        "certTypeId": cert_type_id,
        "certNbr": cert_number,
        "custName": cust_name,
        "showTerminatedSubs": "true",
        "zsmart_dubbo_service_name": "QryCustInfoByParamsEx",
        "zsmart_fish_flag": True,
        "zsmart_referer_url": REFERER_URL,
    }

    payload = {
        "ServiceName": "CallOcsDubboService",
        "Data": data,
        "zsmart_origin_menu": None,
    }

    return payload


async def query_subscriber_api(
    page: Page, iframe_frame, csrf_token: str, cert_number: str, cert_type: str, cust_name: str
) -> List[Dict]:
    """
    Query subscriber status via direct API call from the iframe context.
    Returns list of subscriber entries from custQueryResult.
    """
    payload = build_api_payload(cert_number, cert_type, cust_name)
    url = API_URL + "&timestamp=" + str(int(datetime.now().timestamp() * 1000))

    # Run fetch from the iframe context (same origin as the API)
    target = iframe_frame if iframe_frame else page

    try:
        result = await target.evaluate(
            """async (args) => {
                const [url, payload, csrfToken] = args;
                try {
                    const resp = await fetch(url, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-Requested-With': 'XMLHttpRequest',
                            'x-csrf-token': csrfToken,
                        },
                        body: JSON.stringify(payload),
                        credentials: 'include',
                    });
                    const data = await resp.json();
                    return { success: true, data: data };
                } catch (e) {
                    return { success: false, error: e.message };
                }
            }""",
            [url, payload, csrf_token],
        )

        if not result.get("success"):
            print(f"    API error: {result.get('error')}")
            return []

        data = result.get("data", {})

        if not data.get("isSuccess") and not data.get("callServiceSuccess"):
            msg = data.get("Msg") or data.get("message") or data.get("msg") or ""
            print(f"    API failed: {msg}")
            # On first failure, dump payload keys for debugging
            if cert_number == payload.get("certNbr"):
                print(f"    Payload keys: {list(payload.keys())}")
                print(f"    Session keys: {list(payload.get('zsmartSession', {}).keys())}")
            return []

        results = data.get("custQueryResult", []) or []

        # If results have empty prodStateName, try the second API (qrySubsPageTree)
        if results:
            has_empty_status = any(not r.get("prodStateName") for r in results)
            if has_empty_status:
                # Get custId from first result
                cust_id = results[0].get("custId", "")
                if cust_id:
                    subs_results = await query_subs_page_tree(target, csrf_token, cust_id)
                    if subs_results:
                        # Merge: fill empty prodStateName from subsList by matching offerName/subsPlanName
                        subs_by_offer = {}
                        for s in subs_results:
                            key = (s.get("subsPlanName") or s.get("offerName") or "").lower()
                            if key and s.get("prodStateName"):
                                subs_by_offer[key] = s.get("prodStateName")
                        # Also index by a general status (if all are same)
                        all_states = [s.get("prodStateName") for s in subs_results if s.get("prodStateName")]
                        general_state = all_states[0] if all_states and len(set(all_states)) == 1 else ""

                        for r in results:
                            if not r.get("prodStateName"):
                                offer = (r.get("offerName") or "").lower()
                                if offer in subs_by_offer:
                                    r["prodStateName"] = subs_by_offer[offer]
                                elif general_state:
                                    r["prodStateName"] = general_state

        return results

    except Exception as e:
        print(f"    API call error: {e}")
        return []


SUBS_PAGE_TREE_URL = "https://dealer.unifi.com.my/esales/FishModule/crm/api/subs/qrySubsPageTree"


async def _query_subs_page(target, csrf_token: str, cust_id: str, page_index: int = 1, page_count: int = 50) -> Tuple[List[Dict], bool]:
    """
    Query a single page of subscriber results.
    Returns (subs_list, has_more).
    """
    payload = {
        "requestParam": {
            "custId": str(cust_id),
            "PAGE_REQ": {
                "COUNT_FLAG": "N",
                "PAGE_MODE": "S",
                "pageIndex": page_index,
                "pageCount": page_count,
            },
        }
    }

    try:
        result = await target.evaluate(
            """async (args) => {
                const [url, payload, csrfToken] = args;
                try {
                    const resp = await fetch(url, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-Requested-With': 'XMLHttpRequest',
                            'x-csrf-token': csrfToken,
                        },
                        body: JSON.stringify(payload),
                        credentials: 'include',
                    });
                    const data = await resp.json();
                    return { success: true, data: data };
                } catch (e) {
                    return { success: false, error: e.message };
                }
            }""",
            [SUBS_PAGE_TREE_URL, payload, csrf_token],
        )

        if not result.get("success"):
            return [], False

        data = result.get("data", {})
        if not data.get("isSuccess"):
            return [], False

        subs_list = data.get("subsList", []) or []
        has_more = len(subs_list) >= page_count

        return subs_list, has_more

    except Exception:
        return [], False


async def query_subs_page_tree(target, csrf_token: str, cust_id: str) -> List[Dict]:
    """
    Query subscriber page tree by custId. Paginates automatically if more than 50 results.
    """
    all_subs = []
    page_index = 1
    max_pages = 10  # Safety limit

    while page_index <= max_pages:
        subs, has_more = await _query_subs_page(target, csrf_token, cust_id, page_index)

        if not subs:
            break

        # Log fields on first result
        if page_index == 1 and not hasattr(query_subs_page_tree, "_logged_fields"):
            query_subs_page_tree._logged_fields = True
            sample = subs[0]
            print(f"    [DEBUG] subsList entry keys: {list(sample.keys())}")
            addr_keys = [k for k in sample.keys() if "addr" in k.lower() or "address" in k.lower() or "install" in k.lower()]
            if addr_keys:
                print(f"    [DEBUG] Address-related fields: {addr_keys}")
                for k in addr_keys:
                    print(f"    [DEBUG]   {k} = {sample.get(k, '')!r}")

        all_subs.extend(subs)

        if not has_more:
            break

        page_index += 1
        if page_index > 1:
            print(f"    Fetching page {page_index} ({len(all_subs)} entries so far)")

    return all_subs


async def query_via_order_detail(target, context, csrf_token: str, order_id: str) -> List[Dict]:
    """
    Last resort fallback: open order detail to get custId, then call qrySubsPageTree.
    Returns subsList entries with prodStateName.
    """
    captured = {}

    async def intercept(response):
        try:
            if "getCeeOrderDetail" in response.url and response.status == 200:
                data = await response.json()
                if isinstance(data, dict) and data.get("data"):
                    cust_info = data["data"].get("custInfo", {}) or {}
                    captured["custId"] = cust_info.get("custId", "")
        except Exception:
            pass

    p = await context.new_page()
    p.on("response", intercept)

    try:
        url = f"https://dealer.unifi.com.my/esales/h5/onBoarding/OrderDetails?custOrderId={order_id}&custOrderNbr={order_id}"
        await p.goto(url, wait_until="networkidle", timeout=60000)

        for _ in range(20):
            if "custId" in captured:
                break
            await p.wait_for_timeout(500)

        await p.wait_for_timeout(500)
    except Exception as e:
        print(f"    Error fetching order detail: {e}")
    finally:
        try:
            await p.close()
        except Exception:
            pass

    cust_id = captured.get("custId", "")
    if not cust_id:
        return []

    print(f"    Got custId: {cust_id}")
    subs = await query_subs_page_tree(target, csrf_token, cust_id)
    return subs


_INACTIVE_STATES = {"Transfer Out", "Terminated", "Suspended", ""}


def _all_inactive(results: List[Dict]) -> bool:
    """Check if all subscriber results have inactive statuses."""
    if not results:
        return True
    return all(
        (r.get("prodStateName", "") or PROD_STATE_MAP.get(r.get("prodState", ""), ""))
        in _INACTIVE_STATES
        for r in results
    )


def _get_status(entry: Dict) -> str:
    """Extract status from a result entry. Returns empty string if no status."""
    return entry.get("prodStateName", "") or PROD_STATE_MAP.get(
        entry.get("prodState", ""), ""
    )


def _normalize_address(addr: str) -> str:
    """Normalize address for comparison: lowercase, strip punctuation/whitespace."""
    if not addr:
        return ""
    import re as _re
    # Lowercase, collapse whitespace, remove commas/dots/dashes
    addr = addr.lower().strip()
    addr = _re.sub(r"[,.\-/]", " ", addr)
    addr = _re.sub(r"\s+", " ", addr)
    return addr


def _address_match_score(addr1: str, addr2: str) -> float:
    """
    Score how well two addresses match (0.0 = no match, 1.0 = exact).
    Uses word overlap ratio.
    """
    if not addr1 or not addr2:
        return 0.0
    words1 = set(_normalize_address(addr1).split())
    words2 = set(_normalize_address(addr2).split())
    if not words1 or not words2:
        return 0.0
    overlap = words1 & words2
    # Use Jaccard-like score but biased toward the shorter set
    return len(overlap) / min(len(words1), len(words2))


def _extract_status_date(entry: Dict) -> str:
    """Extract and format the status date from an entry."""
    date = entry.get("prodStateDate") or entry.get("completedDate") or ""
    if date:
        try:
            from datetime import datetime as _dt
            for fmt in ["%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"]:
                try:
                    dt = _dt.strptime(date, fmt)
                    date = dt.strftime("%d %b %Y")
                    break
                except ValueError:
                    continue
        except Exception:
            pass
    return date


# Common address field names in the subsList API response
_ADDR_FIELDS = [
    "installAddress", "installAddr", "address", "displayAddress",
    "fullAddress", "subsAddr", "serviceAddress",
]


def _get_entry_address(entry: Dict) -> str:
    """Try to extract an address from a subsList entry."""
    for field in _ADDR_FIELDS:
        val = entry.get(field, "")
        if val and isinstance(val, str) and val.strip():
            return val.strip()
    return ""



def _package_match_score(order_pkg: str, entry: Dict) -> float:
    """Check if the order package matches a subscriber entry's plan name."""
    if not order_pkg:
        return 0.0
    order_pkg_lower = order_pkg.lower()
    # Check subsPlanName (e.g. "Unifi Home 100Mbps Premium Value (36M)")
    plan = (entry.get("subsPlanName") or "").lower()
    if plan and order_pkg_lower in plan or plan in order_pkg_lower:
        return 1.0
    # Check offerName as fallback
    offer = (entry.get("offerName") or "").lower()
    if offer and order_pkg_lower in offer or offer in order_pkg_lower:
        return 0.8
    # Treat all UNI5G Postpaid variants as equivalent (39, 69, 99, etc.)
    if order_pkg_lower.startswith("uni5g postpaid") and plan.startswith("uni5g postpaid"):
        return 0.9
    if order_pkg_lower.startswith("uni5g postpaid") and offer.startswith("uni5g postpaid"):
        return 0.7
    # Word overlap
    pkg_words = set(order_pkg_lower.split())
    plan_words = set(plan.split())
    if pkg_words and plan_words:
        overlap = len(pkg_words & plan_words) / min(len(pkg_words), len(plan_words))
        if overlap > 0.5:
            return overlap * 0.7
    return 0.0


def match_status_from_api(results: List[Dict], order_address: str = "", order_package: str = "") -> Tuple[str, str]:
    """
    Find the subscriber status from API results.
    Priority: package match first (identifies the service), address as tiebreaker.
    A customer can move addresses but the package stays the same.
    Returns (status, status_date) tuple.
    """
    if not results:
        return "Not Found", ""

    # Step 1: Find entries matching the package
    if order_package:
        pkg_matches = []
        for r in results:
            pkg_score = _package_match_score(order_package, r)
            status = _get_status(r)
            if pkg_score > 0.5 and status:
                pkg_matches.append((r, pkg_score))

        if pkg_matches:
            if len(pkg_matches) > 1:
                # Sort by: address score (desc), then prefer Active over inactive
                def _rank(item):
                    r, pkg_score = item
                    addr_score = _address_match_score(order_address, _get_entry_address(r)) if order_address else 0
                    is_active = 1 if _get_status(r) == "Active" else 0
                    return (addr_score, is_active)

                pkg_matches.sort(key=_rank, reverse=True)

            best_r, best_score = pkg_matches[0]
            status = _get_status(best_r)
            date = _extract_status_date(best_r)
            plan = best_r.get("subsPlanName", "")
            print(f"    Matched by package: {plan[:50]}")
            return status, date

    # Step 2: No package match — fall back to address matching
    if order_address:
        addr_matches = []
        for r in results:
            entry_addr = _get_entry_address(r)
            if not entry_addr:
                continue
            addr_score = _address_match_score(order_address, entry_addr)
            status = _get_status(r)
            if addr_score > 0.5 and status:
                addr_matches.append((r, addr_score))

        if addr_matches:
            best_r, best_score = max(addr_matches, key=lambda x: x[1])
            status = _get_status(best_r)
            date = _extract_status_date(best_r)
            entry_addr = _get_entry_address(best_r)
            print(f"    Matched by address (score={best_score:.2f}): {entry_addr[:60]}")
            return status, date
        else:
            entry_addrs = [_get_entry_address(r) for r in results if _get_entry_address(r)]
            if entry_addrs and len(results) > 1:
                print(f"    No address match found (order: {order_address[:50]}...)")
                for ea in entry_addrs[:3]:
                    print(f"      vs: {ea[:60]}")

    # If we had a package to match but nothing matched, don't pick an unrelated service
    if order_package and results:
        print(f"    No matching package found for: {order_package[:50]}")
        return "No Matching Package", ""

    # Step 3: Last fallback — first entry with a non-empty status (only when no package specified)
    for r in results:
        status = _get_status(r)
        if status:
            date = _extract_status_date(r)
            return status, date

    return "", ""


class StatusBatchWriter:
    """Collects status updates and flushes them in batches to avoid rate limits."""

    def __init__(self, ws, headers: list, batch_size: int = 100):
        self.ws = ws
        self.batch_size = batch_size
        self.pending = []
        self.write_failures = 0
        self.write_errors = []

        try:
            self.status_col = headers.index("Status") + 1  # 1-based
        except ValueError:
            self.status_col = -1

        try:
            self.date_col = headers.index("Status Latest Date") + 1
        except ValueError:
            self.date_col = -1

        try:
            self.time_col = headers.index("Status Scrape Date") + 1
        except ValueError:
            self.time_col = -1

        try:
            self.cust_id_col = headers.index("Cust ID") + 1
        except ValueError:
            self.cust_id_col = -1

    def add(self, row_index: int, order_number: str, status: str,
            status_date: str = "", new_cust_id: str = ""):
        if self.status_col == -1:
            print("    Status column not found in headers")
            return
        if not status:
            status = "-"
        timestamp = "'" + datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
        self.pending.append(
            (row_index, order_number, status, status_date, timestamp, new_cust_id)
        )
        if len(self.pending) >= self.batch_size:
            self.flush()

    def flush(self):
        if not self.pending or self.status_col == -1:
            return

        from gspread.utils import rowcol_to_a1

        batch = []
        for row_index, _order_number, status, status_date, timestamp, new_cust_id in self.pending:
            batch.append({"range": rowcol_to_a1(row_index, self.status_col), "values": [[status]]})
            if self.date_col != -1:
                batch.append({"range": rowcol_to_a1(row_index, self.date_col), "values": [[status_date]]})
            if self.time_col != -1:
                batch.append({"range": rowcol_to_a1(row_index, self.time_col), "values": [[timestamp]]})
            if new_cust_id and self.cust_id_col != -1:
                batch.append({"range": rowcol_to_a1(row_index, self.cust_id_col), "values": [[new_cust_id]]})

        # Retry with exponential backoff so a transient rate-limit (429),
        # 5xx, or read-timeout doesn't permanently drop the whole batch.
        from gspread.exceptions import APIError

        max_attempts = 5
        delay = 2
        last_err = None
        for attempt in range(1, max_attempts + 1):
            try:
                self.ws.batch_update(batch, value_input_option="USER_ENTERED")
                last_err = None
                break
            except APIError as e:
                last_err = e
                code = getattr(getattr(e, "response", None), "status_code", None)
                if code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                    print(f"    ⏳ Sheets API {code}, retry {attempt}/{max_attempts - 1} in {delay}s...")
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                break
            except Exception as e:
                # Network / read-timeout / connection errors — retry too.
                last_err = e
                if attempt < max_attempts:
                    print(f"    ⏳ Sheets write error ({type(e).__name__}), retry {attempt}/{max_attempts - 1} in {delay}s...")
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                break

        if last_err is not None:
            self.write_failures += len(self.pending)
            err_msg = str(last_err)
            if err_msg not in self.write_errors:
                self.write_errors.append(err_msg)
            print(f"    Batch write error ({len(self.pending)} rows) after {max_attempts} attempts: {last_err}")
            self.pending = []
            return

        # Sheets is authoritative and it accepted the batch, so mirror
        # the same rows into Neon. Anything without an order number is
        # skipped: the sheet is keyed by row index, Neon is not.
        neon_writer.update_order_statuses(
            [
                StatusUpdate(order_number, status, status_date, new_cust_id)
                for _row_index, order_number, status, status_date, _timestamp, new_cust_id
                in self.pending
                if order_number
            ]
        )

        self.pending = []


### --- PLAN B: UI SCRAPING FALLBACK --- ###


# Map cert type names to dropdown option text
CERT_TYPE_UI_MAP = {
    "MyKad": "MyKad",
    "Passport": "Passport",
    "Police/Army": "Police/Army",
    "Old IC": "Old IC",
    "Birth Cert": "Birth Cert",
    "Business Reg": "Company with Business Registration Number",
    "Company with Business Registration Number": "Company with Business Registration Number",
    "Company without Business Registration Number": "Company without Business Registration Nu",
    "Channel Account": "Channel Account",
    "I-KAD": "I-KAD",
    "Internal Division ID": "Internal Division ID",
    "MyKAS": "MyKAS",
    "Others": "Others",
}


async def select_id_type_ui(frame, cert_type: str):
    """Select the ID Type from the dropdown in the Advanced Query form."""
    if not cert_type:
        return

    ui_type = CERT_TYPE_UI_MAP.get(cert_type, cert_type)

    try:
        # Try <select> element first (the dropdown in the Advanced Query form)
        select_el = frame.locator('select.js-cert-type-id, select[name="certTypeId"]')
        if await select_el.count() > 0:
            await select_el.first.select_option(label=ui_type, timeout=5000)
            print(f"    Selected ID Type: {ui_type}")
            return

        # Fallback: try clicking the dropdown trigger and selecting from list
        dropdown = frame.locator(
            "div.form-group.js-cert-type-id-content span.glyphicon-triangle-bottom"
        )
        if await dropdown.count() == 0:
            dropdown = frame.locator('.js-cert-type-id-content .btn-group .btn')

        await dropdown.first.click(timeout=5000)
        await frame.wait_for_timeout(1000)

        option = frame.locator(
            f'a:has-text("{ui_type}"), li:has-text("{ui_type}")'
        ).first
        if await option.count() > 0:
            await option.click(timeout=5000)
            await frame.wait_for_timeout(500)
            print(f"    Selected ID Type: {ui_type}")
        else:
            print(f"    Could not find ID Type option: {ui_type}")

    except Exception as e:
        print(f"    Error selecting ID Type: {e}")


async def query_subscriber_ui(
    page: Page, iframe_frame, cert_number: str, cert_type: str, name: str
) -> List[Dict]:
    """
    Plan B: Fill the Advanced Query form via UI and intercept the API response.
    Returns the custQueryResult list from the intercepted response.
    iframe_frame: the iframe frame to interact with (or None to auto-detect).
    form_already_open: if True, skip clicking >> (just fill and query directly).
    """
    # Find the iframe if not provided
    if iframe_frame is None:
        iframe_frame = page
        for f in page.frames:
            if f == page.main_frame:
                continue
            if "remote.html" in f.url or "orderentry" in f.url.lower():
                iframe_frame = f
                break

    captured = {}

    async def intercept_response(response):
        try:
            if (
                "callservice.json" in response.url
                and "CallOcsDubboService" in response.url
                and response.status == 200
            ):
                try:
                    data = await response.json()
                    if isinstance(data, dict) and "custQueryResult" in data:
                        captured["results"] = data.get("custQueryResult", [])
                except Exception:
                    pass
        except Exception:
            pass

    page.on("response", intercept_response)

    for attempt in range(2):
        try:
            # Check if form is still open, reopen if needed
            cert_input = iframe_frame.locator('input[name="certNbr"]')
            if await cert_input.count() == 0 or not await cert_input.first.is_visible():
                print(f"    Form not visible, reopening...")
                try:
                    await iframe_frame.locator("div.js-advanced-query-btn").first.click(timeout=10000)
                    await page.wait_for_timeout(3000)
                except Exception:
                    pass

            # Select ID Type
            await select_id_type_ui(iframe_frame, cert_type)

            await iframe_frame.locator('input[name="certNbr"]').fill(cert_number, timeout=10000)
            await page.wait_for_timeout(300)

            await iframe_frame.locator('input[name="custName"]').fill(name, timeout=10000)
            await page.wait_for_timeout(300)

            await iframe_frame.locator("button.js-query").click(timeout=10000)

            for _ in range(20):
                if "results" in captured:
                    break
                await page.wait_for_timeout(500)

            await page.wait_for_timeout(1000)
            break  # success, no need to retry

        except Exception as e:
            if attempt == 0:
                print(f"    Attempt 1 failed, retrying: {e}")
                await page.wait_for_timeout(2000)
            else:
                print(f"    Error querying {cert_number}: {e}")
                captured["error"] = True
                try:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(1000)
                except Exception:
                    pass

    page.remove_listener("response", intercept_response)

    if captured.get("error"):
        return None  # Signal that this was an error, not "Not Found"

    return captured.get("results", [])


async def check_all_statuses(
    page: Page,
    month_text: str,
    year: int,
    ws=None,
    iframe_frame=None,
    only_empty: bool = False,
) -> Dict:
    """
    Check subscriber statuses via qrySubsPageTree using custId from the sheet.
    No Advanced Query needed — just direct API calls.

    Args:
        page: Playwright page (already logged in)
        month_text: Month name (e.g. 'Jun')
        year: Year (e.g. 2026)
        ws: Optional worksheet object. If None, opens the sheet.
        iframe_frame: iframe frame for API calls (reused across months)
        only_empty: If True, only check orders without a Status value.
    """
    print("\n" + "=" * 70)
    print(f"SUBSCRIBER STATUS CHECK — {month_text} {year}")
    print("=" * 70)

    # Open sheet if not provided
    if ws is None:
        spread = open_sheet()
        tab_title = month_tab_title(month_text, year)
        try:
            ws = spread.worksheet(tab_title)
        except Exception:
            print(f"  Tab '{tab_title}' not found — skipping")
            return {"total": 0, "checked": 0, "not_found": 0, "errors": 0, "skipped": True}
        from gsheets_writer import HEADERS
        first_row = ws.row_values(1)
        if first_row != HEADERS:
            ws.update([HEADERS], "A1")
            print("  Updated sheet headers with new columns")
        print(f"  Sheet tab: {tab_title}")
    else:
        from gsheets_writer import HEADERS
        first_row = ws.row_values(1)
        if first_row != HEADERS:
            ws.update([HEADERS], "A1")
            print("  Updated sheet headers with new columns")

    # Get orders to check
    orders, cancelled_rows = get_orders_to_check(ws, only_empty=only_empty)
    print(f"  Orders to check: {len(orders)}")

    # Cache the headers once
    sheet_headers = ws.row_values(1)
    writer = StatusBatchWriter(ws, sheet_headers)

    # Fill cancelled orders with "-" immediately
    if cancelled_rows:
        print(f"  Writing '-' for {len(cancelled_rows)} cancelled orders...")
        for row_idx, order_number in cancelled_rows:
            writer.add(row_idx, order_number, "-")
        writer.flush()
        print(f"  Done marking cancelled orders")

    if not orders:
        print("  No orders need status checking")
        return {"total": 0, "checked": 0, "not_found": 0, "errors": 0, "cancelled": len(cancelled_rows)}

    # Navigate to Order Entry to get iframe for API calls (skip if already provided)
    if iframe_frame is None:
        iframe_frame = await navigate_to_order_entry(page)

    # Capture CSRF token — try multiple methods
    csrf_token = ""

    # Method 1: Extract from cookies/meta via JS in the iframe
    try:
        csrf_token = await iframe_frame.evaluate("""() => {
            // Check meta tags
            const meta = document.querySelector('meta[name="_csrf"]') ||
                         document.querySelector('meta[name="csrf-token"]');
            if (meta) return meta.getAttribute('content') || '';
            // Check if jQuery has it
            if (typeof $ !== 'undefined' && $.ajaxSettings && $.ajaxSettings.headers)
                return $.ajaxSettings.headers['X-CSRF-TOKEN'] || '';
            return '';
        }""")
    except Exception:
        pass

    # Method 2: Do a dummy UI query and intercept the request
    if not csrf_token:
        captured_csrf = {}

        async def capture_csrf(request):
            try:
                if "callservice.json" in request.url or "qrySubsPageTree" in request.url:
                    c = request.headers.get("x-csrf-token", "")
                    if c:
                        captured_csrf["token"] = c
            except Exception:
                pass

        page.on("request", capture_csrf)
        try:
            await iframe_frame.locator('input[name="certNbr"]').fill("000000000000", timeout=10000)
            await page.wait_for_timeout(300)
            await iframe_frame.locator('input[name="custName"]').fill("TEST", timeout=5000)
            await page.wait_for_timeout(300)
            await iframe_frame.locator("button.js-query").click(timeout=5000)
            await page.wait_for_timeout(3000)
        except Exception:
            pass
        page.remove_listener("request", capture_csrf)
        csrf_token = captured_csrf.get("token", "")

    # Method 3: Extract from page cookies
    if not csrf_token:
        try:
            cookies = await page.context.cookies()
            for c in cookies:
                if "csrf" in c["name"].lower():
                    csrf_token = c["value"]
                    break
        except Exception:
            pass

    # Method 4: Use the first order's custId to call qrySubsPageTree and intercept CSRF
    if not csrf_token:
        captured_csrf2 = {}

        async def capture_csrf2(request):
            try:
                csrf = request.headers.get("x-csrf-token", "")
                if csrf:
                    captured_csrf2["token"] = csrf
            except Exception:
                pass

        page.on("request", capture_csrf2)
        # Make any API call to the server — just load a page in the iframe
        try:
            await iframe_frame.evaluate("""async () => {
                try {
                    await fetch('/esales/FishModule/crm/api/subs/qrySubsPageTree', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                        body: JSON.stringify({ requestParam: { custId: '0', PAGE_REQ: { COUNT_FLAG: 'N', PAGE_MODE: 'S', pageIndex: 1, pageCount: 1 } } }),
                        credentials: 'include',
                    });
                } catch(e) {}
            }""")
            await page.wait_for_timeout(2000)
        except Exception:
            pass
        page.remove_listener("request", capture_csrf2)
        csrf_token = captured_csrf2.get("token", "")

    # Method 5: Search all cookies and session storage
    if not csrf_token:
        try:
            csrf_token = await iframe_frame.evaluate("""() => {
                // Check sessionStorage
                for (let i = 0; i < sessionStorage.length; i++) {
                    const key = sessionStorage.key(i);
                    if (key.toLowerCase().includes('csrf')) return sessionStorage.getItem(key);
                }
                // Check localStorage
                for (let i = 0; i < localStorage.length; i++) {
                    const key = localStorage.key(i);
                    if (key.toLowerCase().includes('csrf')) return localStorage.getItem(key);
                }
                // Check all script-accessible cookies
                const cookies = document.cookie.split(';');
                for (const c of cookies) {
                    if (c.trim().toLowerCase().includes('csrf')) return c.split('=')[1] || '';
                }
                // Check window variables
                if (window._csrf) return window._csrf;
                if (window.csrfToken) return window.csrfToken;
                // Check for zsmart framework token
                try { if (window.zsmart && window.zsmart.csrf) return window.zsmart.csrf; } catch(e) {}
                try { if (window.Fish && window.Fish.csrf) return window.Fish.csrf; } catch(e) {}
                return '';
            }""") or ""
        except Exception:
            pass

    if not csrf_token:
        # Last resort: extract from any XHR setup in the iframe's jQuery/axios
        try:
            csrf_token = await iframe_frame.evaluate("""() => {
                // jQuery
                try {
                    if (typeof jQuery !== 'undefined') {
                        const h = jQuery.ajaxSettings.headers || {};
                        return h['X-CSRF-TOKEN'] || h['x-csrf-token'] || '';
                    }
                } catch(e) {}
                // Axios
                try {
                    if (typeof axios !== 'undefined') {
                        return axios.defaults.headers.common['X-CSRF-TOKEN'] ||
                               axios.defaults.headers.common['x-csrf-token'] || '';
                    }
                } catch(e) {}
                return '';
            }""") or ""
        except Exception:
            pass

    if not csrf_token:
        print("  CSRF token not captured — cannot proceed")
        print("  Debug: try running backfill_custid first, or check if Order Entry page loaded correctly")
        return {"total": len(orders), "checked": 0, "not_found": 0, "errors": len(orders)}

    print(f"  CSRF captured — querying via qrySubsPageTree (fast)")

    checked = 0
    not_found = 0
    errors = 0

    # Deduplicate: group orders by custId
    cust_groups: Dict[str, List[Dict]] = {}
    for order in orders:
        cid = order["cust_id"]
        if cid not in cust_groups:
            cust_groups[cid] = []
        cust_groups[cid].append(order)

    print(f"  Unique customers: {len(cust_groups)}")
    print(f"  (Deduplicated from {len(orders)} orders)")

    target = iframe_frame if iframe_frame else page
    query_num = 0
    for cust_id, group_orders in cust_groups.items():
        query_num += 1
        order_nums = [o["order_number"] for o in group_orders]
        print(f"\n  [{query_num}/{len(cust_groups)}] custId={cust_id}")
        print(f"    Orders: {', '.join(order_nums)}")

        try:
            results = await query_subs_page_tree(target, csrf_token, cust_id)
            print(f"    {len(results)} subscriber entries")

            # Fallback: if all results are inactive (Transfer Out / Terminated),
            # try IC lookup to find a newer custId with active service
            updated_cust_id = ""  # Track if we found a new custId
            if _all_inactive(results):
                # Use IC number from the first order that has one
                ic_field = ""
                name = ""
                for o in group_orders:
                    if o.get("ic_number"):
                        ic_field = o["ic_number"]
                        name = o.get("name", "")
                        break

                if ic_field:
                    cert_number, cert_type = parse_ic_number(ic_field)
                    if cert_number:
                        print(f"    All inactive — trying IC lookup: {cert_number} ({cert_type})")
                        ic_results = await query_subscriber_api(
                            page, target, csrf_token, cert_number, cert_type, name
                        )
                        if ic_results:
                            # Find new custIds from IC lookup
                            new_cust_ids = set()
                            for r in ic_results:
                                new_cid = r.get("custId", "")
                                if new_cid and new_cid != cust_id:
                                    new_cust_ids.add(new_cid)

                            # Prefer non-10XXX custIds (10XXX is the old format)
                            new_cust_ids = sorted(
                                new_cust_ids,
                                key=lambda x: (x.startswith("10"), x),
                            )

                            # Query each new custId and collect results
                            for new_cid in new_cust_ids:
                                new_results = await query_subs_page_tree(target, csrf_token, new_cid)
                                if new_results and not _all_inactive(new_results):
                                    print(f"    Found active custId: {new_cid} ({len(new_results)} entries)")
                                    results = new_results
                                    updated_cust_id = new_cid
                                    break

            for order in group_orders:
                order_addr = order.get("address", "")
                order_pkg = order.get("package", "")
                order_results = results
                order_updated_cust_id = updated_cust_id

                status, status_date = match_status_from_api(order_results, order_address=order_addr, order_package=order_pkg)

                # If status is inactive, try IC lookup to find the service under a different custId
                if order_pkg and status in ("Terminated", "Transfer Out", "Not Found", "No Matching Package", ""):
                    # Only skip IC lookup if there's an Active package match already
                    has_active_pkg_match = any(
                        _package_match_score(order_pkg, r) > 0.5 and _get_status(r) == "Active"
                        for r in order_results
                    )
                    if not has_active_pkg_match:
                        ic_field = order.get("ic_number", "")
                        name = order.get("name", "")
                        if ic_field:
                            cert_number, cert_type = parse_ic_number(ic_field)
                            if cert_number:
                                print(f"    No package match — trying IC lookup: {cert_number}")
                                ic_results = await query_subscriber_api(
                                    page, target, csrf_token, cert_number, cert_type, name
                                )
                                if ic_results:
                                    # Find custIds that have the matching package
                                    for r in ic_results:
                                        new_cid = r.get("custId", "")
                                        if new_cid and new_cid != cust_id:
                                            new_subs = await query_subs_page_tree(target, csrf_token, new_cid)
                                            if new_subs:
                                                new_status, new_date = match_status_from_api(
                                                    new_subs, order_address=order_addr, order_package=order_pkg
                                                )
                                                if new_status == "Active":
                                                    status = new_status
                                                    status_date = new_date
                                                    order_updated_cust_id = new_cid
                                                    break

                display_status = status if status else "-"
                print(f"    {order['order_number']} -> {display_status} ({status_date})")
                if order_updated_cust_id:
                    print(f"    Cust ID updated: {cust_id} -> {order_updated_cust_id}")
                writer.add(order["row_index"], order["order_number"], status, status_date, new_cust_id=order_updated_cust_id)

                if status == "Not Found":
                    not_found += 1
                else:
                    checked += 1

        except Exception as e:
            print(f"    Error: {e}")
            errors += len(group_orders)

    # Flush remaining writes
    writer.flush()

    # Summary
    print("\n" + "=" * 70)
    print("STATUS CHECK SUMMARY")
    print("=" * 70)
    print(f"  Total orders: {len(orders)}")
    print(f"  Unique customers: {len(cust_groups)}")
    print(f"  Status found: {checked}")
    print(f"  Not Found: {not_found}")
    print(f"  Errors: {errors}")
    if writer.write_failures > 0:
        print(f"  ⚠️ WRITE FAILURES: {writer.write_failures} rows failed to save!")
        for err in writer.write_errors:
            print(f"    -> {err[:100]}")
    print("=" * 70)

    return {
        "total": len(orders),
        "checked": checked,
        "not_found": not_found,
        "errors": errors,
        "write_failures": writer.write_failures,
    }


async def check_status_standalone(
    username: str,
    password: str,
    month_text: str,
    year: int,
) -> Dict:
    """
    Standalone entry point: logs in, checks statuses for one month, closes browser.
    """
    from login_manager import login_and_get_context

    browser, context, pw, page = await login_and_get_context(username, password)

    try:
        result = await check_all_statuses(page, month_text, year)
        return result
    finally:
        await context.close()
        await browser.close()
        await pw.stop()


async def check_status_standalone_empty(
    username: str,
    password: str,
    month_text: str,
    year: int,
) -> Dict:
    """
    Standalone entry point: only checks orders that don't have a Status yet.
    """
    from login_manager import login_and_get_context

    browser, context, pw, page = await login_and_get_context(username, password)

    try:
        result = await check_all_statuses(page, month_text, year, only_empty=True)
        return result
    finally:
        await context.close()
        await browser.close()
        await pw.stop()


async def check_status_multi_month(
    username: str,
    password: str,
    months: list,
) -> Dict:
    """
    Check statuses for multiple months in one login session.
    Args:
        months: list of (month_text, year) tuples, e.g. [("Jun", 2026), ("May", 2026), ...]
    """
    from login_manager import login_and_get_context

    browser, context, pw, page = await login_and_get_context(username, password)

    all_results = {}
    try:
        # Navigate to Order Entry once — reused across all months
        iframe_frame = await navigate_to_order_entry(page)

        for month_text, year in months:
            print(f"\n{'#' * 70}")
            print(f"# CHECKING: {month_text} {year}")
            print(f"{'#' * 70}")
            try:
                result = await check_all_statuses(page, month_text, year, iframe_frame=iframe_frame)
                all_results[f"{month_text} {year}"] = result
            except Exception as e:
                print(f"  Error checking {month_text} {year}: {e}")
                all_results[f"{month_text} {year}"] = {"error": str(e)}
    finally:
        await context.close()
        await browser.close()
        await pw.stop()

    # Print overall summary
    print(f"\n{'=' * 70}")
    print("MULTI-MONTH STATUS CHECK COMPLETE")
    print(f"{'=' * 70}")
    for key, result in all_results.items():
        if "error" in result:
            print(f"  {key}: ERROR - {result['error']}")
        else:
            print(f"  {key}: {result.get('checked', 0)} checked, {result.get('not_found', 0)} not found, {result.get('errors', 0)} errors")
    print(f"{'=' * 70}")

    return all_results


def get_last_n_months(n: int) -> list:
    """
    Return list of (month_text, year) tuples for the last N months including current.
    E.g. if now is Jun 2026 and n=6: [("Jun", 2026), ("May", 2026), ..., ("Jan", 2026)]
    """
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    now = datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.now()
    months = []
    for i in range(n):
        month_idx = now.month - 1 - i  # 0-based
        year = now.year
        while month_idx < 0:
            month_idx += 12
            year -= 1
        months.append((month_names[month_idx], year))
    return months


def check_status_sync(month_text: str, year: int) -> Dict:
    """
    Synchronous wrapper — loads credentials and runs the status check.
    """
    import asyncio

    from credential_manager import CredentialManager

    cred_manager = CredentialManager()
    if not cred_manager.credentials_exist():
        return {"success": False, "error": "No credentials saved"}

    creds = cred_manager.get_credentials()
    username = creds.get("username")
    password = creds.get("password")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            check_status_standalone(username, password, month_text, year)
        )
        return result
    finally:
        loop.close()
