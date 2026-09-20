"""Write scraped Unifi orders to Neon.

Google Sheets stays authoritative for the whole dual-write period, so
nothing in this module may raise into a scrape. Every public function
catches, counts the failure and returns.

The row dicts this module accepts are the same ones gsheets_writer takes
-- keyed by the sheet's column headers. coerce_row() is the only place
that knows about the sheet's string formats.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Kuala_Lumpur")

# Ordered most-specific first. These are the formats the existing code
# actually emits; see scrape_orders.format_datetime,
# date_utils.standardize_date and check_status._extract_status_date.
_DATE_FORMATS = (
    "%d %b %Y %H:%M:%S",
    "%d %b %Y %H:%M",
    "%d %b %Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y%m%d%H%M%S",
)


def text(value):
    """Trim, drop the sheet's leading apostrophe, blank -> None."""
    if value is None:
        return None
    cleaned = str(value).strip().lstrip("'").strip()
    return cleaned or None


def status_text(value):
    """As text(), but the sheet's '-' sentinel becomes None.

    '-' means "this order is cancelled, don't query it". It is not an
    observed state and must not appear in the status timeline.
    """
    cleaned = text(value)
    return None if cleaned == "-" else cleaned


def parse_dt(value):
    """Parse any date the scraper writes into an aware datetime.

    Returns None for blanks and for anything unrecognised -- a garbage
    cell must not kill a scrape.
    """
    cleaned = text(value)
    if not cleaned:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            continue
    return None


# Sheet header -> (column name, coercion function)
_FIELD_MAP = (
    ("Order Number",       "order_number",       text),
    ("Event Type",         "event_type",         text),
    ("Order Status",       "order_status",       text),
    ("Created Date",       "created_date",       parse_dt),
    ("Updated Date",       "updated_date",       parse_dt),
    ("Org Code",           "org_code",           text),
    ("Organization Name",  "organization_name",  text),
    ("Name",               "customer_name",      text),
    ("Company Name",       "company_name",       text),
    ("Email",              "email",              text),
    ("Phone Number",       "phone_number",       text),
    ("Appointment Date",   "appointment_date",   parse_dt),
    ("Address",            "address",            text),
    ("Package",            "package",            text),
    ("Device",             "device",             text),
    ("IC Number",          "ic_number",          text),
    ("Creator",            "creator",            text),
    ("Cust ID",            "cust_id",            text),
    ("Status",             "status",             status_text),
    ("Status Latest Date", "status_latest_date", parse_dt),
    ("Status Scrape Date", "status_scrape_date", parse_dt),
)


def coerce_row(row: dict) -> dict:
    """Turn one Sheet-shaped row dict into typed column values.

    `raw` comes back as a plain dict; the caller wraps it in Jsonb at
    bind time so this function stays usable without a database.
    """
    out = {column: fn(row.get(header)) for header, column, fn in _FIELD_MAP}
    out["last_synced"] = parse_dt(row.get("Last Synced")) or datetime.now(LOCAL_TZ)
    out["raw"] = row
    return out
