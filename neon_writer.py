"""Write scraped Unifi orders to Neon.

Google Sheets stays authoritative for the whole dual-write period, so
nothing in this module may raise into a scrape. Every public function
catches, counts the failure and returns.

The row dicts this module accepts are the same ones gsheets_writer takes
-- keyed by the sheet's column headers. coerce_row() is the only place
that knows about the sheet's string formats.
"""

import functools
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

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


_RANGE_SEP = " - "  # space-hyphen-space: scrape_orders.py builds appointment
# ranges as f"{format_datetime(appt_start)} - {format_datetime(appt_end)}".
# Splitting on this exact three-character token (not a bare "-") is what
# keeps it from colliding with the hyphens inside "%Y-%m-%d", which are
# never surrounded by spaces.


def parse_dt(value):
    """Parse any date the scraper writes into an aware datetime.

    Returns None for blanks and for anything unrecognised -- a garbage
    cell must not kill a scrape.

    Appointment cells can hold a range ("start - end", built by
    scrape_orders.py when both an appointment start and end are known).
    When one is seen, only the start is parsed; the full range text is
    still preserved in coerce_row's `raw` column.
    """
    cleaned = text(value)
    if not cleaned:
        return None
    if _RANGE_SEP in cleaned:
        cleaned = cleaned.split(_RANGE_SEP, 1)[0].strip()
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


_pool = None
_pool_lock = threading.Lock()
_failures = 0
_warned_no_url = False
_current_run_id = None


def _get_pool():
    """The pool, or None when DATABASE_URL is unset.

    run_daily.py runs each month's scrape as a SUBPROCESS, so this is
    six short-lived pools in sequence rather than one long-lived pool.
    min_size stays at 1 so we do not open connections that are thrown
    away seconds later. Neon also closes idle connections, which is the
    other reason this is a pool and not one long-lived connection.
    """
    global _pool, _warned_no_url
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        url = os.environ.get("DATABASE_URL")
        if not url:
            if not _warned_no_url:
                print("ℹ️  neon: DATABASE_URL unset — skipping Neon writes")
                _warned_no_url = True
            return None
        # A scraper must not block for psycopg_pool's 30-second default
        # waiting on a database that is merely a nice-to-have. The test
        # suite turns this right down for the outage test.
        timeout = float(os.environ.get("NEON_POOL_TIMEOUT", "10"))
        pool = ConnectionPool(url, min_size=1, max_size=4, timeout=timeout, open=False)
        pool.open()
        _pool = pool
        return _pool


def close():
    """Close the pool. Tests call this; so does a long-lived process on exit."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def write_failure_count() -> int:
    return _failures


def reset_failures():
    global _failures
    _failures = 0


def _guard(fn):
    """Catch, count, carry on. Nothing here may raise into a scrape."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        global _failures
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            _failures += 1
            print(f"⚠️  neon: {fn.__name__} failed: {exc}")
            return 0
    return wrapper


def _apply_run_id(conn):
    """Tag whatever this connection writes with the current run.

    The trigger reads `unifi.scrape_run_id` off the session, which is
    how an event learns which scrape observed it without every INSERT
    having to carry the id.
    """
    if _current_run_id is not None:
        conn.execute(
            "SELECT set_config('unifi.scrape_run_id', %s, true)",
            (str(_current_run_id),),
        )


_ORDER_COLUMNS = (
    "order_number", "event_type", "order_status", "created_date", "updated_date",
    "org_code", "organization_name", "customer_name", "company_name", "email",
    "phone_number", "appointment_date", "address", "package", "device",
    "ic_number", "creator", "cust_id", "status", "status_latest_date",
    "status_scrape_date", "last_synced", "raw",
)

# Columns a scrape always knows and may freely overwrite.
_ALWAYS_UPDATE = (
    "event_type", "order_status", "created_date", "updated_date", "org_code",
    "organization_name", "customer_name", "company_name", "email",
    "phone_number", "appointment_date", "address", "package", "device",
    "ic_number", "creator", "last_synced", "raw",
)

# Columns only check_status and check_custid populate. A scrape that did
# not look must not blank what a check found, so these coalesce.
_COALESCE_UPDATE = ("cust_id", "status", "status_latest_date", "status_scrape_date")

_UPSERT_SQL = (
    "INSERT INTO unifi_orders (" + ", ".join(_ORDER_COLUMNS) + ") VALUES ("
    + ", ".join(f"%({c})s" for c in _ORDER_COLUMNS)
    + ") ON CONFLICT (order_number) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _ALWAYS_UPDATE)
    + ", "
    + ", ".join(
        f"{c} = coalesce(EXCLUDED.{c}, unifi_orders.{c})" for c in _COALESCE_UPDATE
    )
)


@_guard
def upsert_orders(rows) -> int:
    """Insert or update orders. Returns the number of rows sent."""
    if not rows:
        return 0
    pool = _get_pool()
    if pool is None:
        return 0

    params = []
    for row in rows:
        values = coerce_row(row)
        if not values["order_number"]:
            continue
        values["raw"] = Jsonb(values["raw"])
        params.append(values)

    if not params:
        return 0

    with pool.connection() as conn:
        _apply_run_id(conn)
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_SQL, params)
    return len(params)
