"""Apply sql/001_unifi_schema.sql to the database in DATABASE_URL.

An alternative to `psql -f sql/001_unifi_schema.sql` for hosts with no
psql installed. It needs only psycopg, which requirements.txt already
pins for the scraper itself.

The whole file is sent as ONE statement string. psycopg uses Postgres'
simple query protocol when no parameters are passed, which both allows
several semicolon-separated commands and leaves the dollar-quoted
function and DO bodies for the server to parse -- splitting the file on
";" client-side would cut those bodies in half.

The DDL is idempotent (IF NOT EXISTS / OR REPLACE), so re-running is
safe and is the normal way to pick up a schema change.

Usage:
  python apply_schema.py            # apply, then report what exists
  python apply_schema.py --verify   # report only, change nothing
"""

import os
import pathlib
import sys

import psycopg
from dotenv import load_dotenv

load_dotenv()

from sql.checksum import compute

SQL_PATH = pathlib.Path(__file__).resolve().parent / "sql" / "001_unifi_schema.sql"

_EXPECTED_TABLES = (
    "unifi_channels",
    "unifi_orders",
    "unifi_order_status_events",
    "unifi_scrape_runs",
)
_EXPECTED_VIEWS = (
    "unifi_monthly_channel_breakdown",
    "unifi_monthly_stats",
    "unifi_order_status_timeline",
    "unifi_unmapped_channels",
)


def report(conn) -> bool:
    """Print what exists. True when every expected object is present."""
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT tablename FROM pg_tables"
            " WHERE schemaname = 'public' AND tablename LIKE 'unifi\\_%'"
            " ORDER BY tablename"
        ).fetchall()
    ]
    views = [
        r[0]
        for r in conn.execute(
            "SELECT viewname FROM pg_views"
            " WHERE schemaname = 'public' AND viewname LIKE 'unifi\\_%'"
            " ORDER BY viewname"
        ).fetchall()
    ]
    trigger = conn.execute(
        "SELECT tgname FROM pg_trigger WHERE tgname = 'unifi_orders_status_change'"
    ).fetchone()
    role = conn.execute(
        "SELECT rolname FROM pg_roles WHERE rolname = 'unifi_scraper'"
    ).fetchone()

    print(f"  tables  ({len(tables)}/4): {', '.join(tables) or '-'}")
    print(f"  views   ({len(views)}/4): {', '.join(views) or '-'}")
    print(f"  trigger: {'yes' if trigger else 'MISSING'}")
    print(f"  role:    {'yes' if role else 'MISSING'}")

    missing = [t for t in _EXPECTED_TABLES if t not in tables]
    missing += [v for v in _EXPECTED_VIEWS if v not in views]
    if missing:
        print(f"  ⚠️  missing: {', '.join(missing)}")
    if role and not conn.execute(
        "SELECT rolcanlogin AND rolpassword IS NOT NULL FROM pg_authid"
        " WHERE rolname = 'unifi_scraper'"
    ).fetchone()[0]:
        print(
            "  ⚠️  unifi_scraper has no password and cannot log in yet."
            " Run: ALTER ROLE unifi_scraper WITH PASSWORD '<secret>';"
        )
    return not missing and bool(trigger)


def main():
    verify_only = "--verify" in sys.argv[1:]

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is unset. Put it in .env or the environment.")

    print(f"schema checksum: {compute(SQL_PATH)}")

    # autocommit=False: the DDL lands as one transaction, so a failure
    # half way through leaves nothing behind to clean up by hand.
    with psycopg.connect(url) as conn:
        if verify_only:
            print(f"verifying {conn.info.host}/{conn.info.dbname} as {conn.info.user}")
            sys.exit(0 if report(conn) else 1)

        print(f"applying to {conn.info.host}/{conn.info.dbname} as {conn.info.user}")
        conn.execute(SQL_PATH.read_text())
        conn.commit()
        print("applied. now present:")
        ok = report(conn)

    print("done." if ok else "done, but something is missing — see above.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
