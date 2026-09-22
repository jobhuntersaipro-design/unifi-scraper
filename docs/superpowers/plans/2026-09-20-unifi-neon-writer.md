# Unifi Neon Schema and Dual-Write Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Unifi scraper a canonical Postgres schema in Neon and make every write it already does to Google Sheets also land in Neon, without the sheet path changing behaviour.

**Architecture:** `sql/001_unifi_schema.sql` is the canonical DDL — four tables, a generated column, one change-logging trigger and four views. `neon_writer.py` holds a psycopg connection pool and a coercion layer that turns the scraper's Sheet-shaped string dicts into typed rows. `writers.py` is a thin facade the call sites use: Google Sheets stays authoritative and may raise, Neon is a shadow write that never can. A one-time `backfill_neon.py` loads every existing month tab.

**Tech Stack:** Python 3.11+, psycopg 3 (`psycopg[binary,pool]`), pytest, Postgres 16+ (local Docker for tests, Neon for real), gspread (existing).

**Spec:** `docs/superpowers/specs/2026-09-20-unifi-neon-writer-design.md`

## Global Constraints

- **Every database object carries the `unifi_` prefix** — tables, the trigger function, the trigger, the views, the role. The target schema already contains the portal's own `orders`, `order_status_events` and `admin_audit_log`, and trigger functions are schema-scoped in Postgres. An unprefixed `log_order_status_change()` would replace the portal's without erroring.
- **The trigger guard compares exactly three columns:** `order_status`, `status`, `cust_id`. Never `status_scrape_date`, never `last_synced`. Those move on every check; including them writes one event per order per night.
- **Neon writes can never raise into a scrape.** Every public function in `neon_writer.py` catches, counts and returns. Google Sheets remains authoritative for the whole dual-write period.
- **`Status = "-"` coerces to SQL NULL.** It is the sheet's "cancelled, don't check" sentinel, not an observed state.
- **Local timezone is `Asia/Kuala_Lumpur`.** Every date the scraper emits is naive local time and must be given that zone before storing as `timestamptz`.
- **Target database:** project `wifibizz_bill_generator`, branch `production`, database `neondb`, schema `public`. Development happens on a branch off `production`, never on `production` directly.
- **Stat definitions, copied verbatim from the parent spec §5** — these must match the Telegram message to the row:
  - `completed` = `lower(order_status) = 'completed'` (exact)
  - `cancelled` = `order_status` matching `cancel|void|failed` (substring, case-insensitive)
  - everything else counts toward the total only
- **Commit after every task.** Never `git add -A`; name the files.

---

### Task 1: Provisioning, dependencies, and the test harness

Nothing in this plan can be tested until a Postgres exists and pytest can build a throwaway database from a SQL file. This task ends with an empty-but-real schema file and a fixture that applies it.

**Files:**
- Create: `sql/001_unifi_schema.sql`
- Create: `tests/__init__.py` (empty)
- Create: `tests/conftest.py`
- Create: `tests/test_schema.py`
- Create: `.env.example`
- Modify: `requirements.txt`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: pytest fixture `db` — an open `psycopg.Connection` (autocommit) to a fresh database with `sql/001_unifi_schema.sql` applied, dropped on teardown. Every later DB test uses it.

- [ ] **Step 1: Provision Postgres for development**

Two things must exist before any code runs. Do these by hand and record the results.

**a. A local Postgres for tests.** This is what the test suite talks to:

```bash
docker run -d --name unifi-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16
```

If Docker is not available, create a scratch Neon branch instead and export its URL as `TEST_DATABASE_URL`. The fixture takes whichever it finds.

**b. A Neon development branch.** In the Neon console for project `wifibizz_bill_generator`, create a branch off `production`. All schema iteration happens there. Do **not** apply this SQL to `production` — that only happens later, in the same sitting as the portal's `prisma migrate resolve --applied`, because between the apply and the resolve anyone running `prisma migrate dev` may be offered a database reset.

**c. Before writing any SQL, check the collision assumption.** In the Neon SQL Editor against `production`:

```sql
SELECT p.proname, n.nspname
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public' AND p.proname ILIKE '%status%';

SELECT tgname, relname
FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
WHERE NOT t.tgisinternal;
```

Record the output in the commit message. If anything is already named `unifi_log_order_status_change` or `unifi_orders_status_change`, stop and raise it — this plan assumes those names are free.

- [ ] **Step 2: Add the dependencies**

Append to `requirements.txt`:

```
psycopg[binary,pool]==3.2.*
pytest==8.*
```

Install: `pip install -r requirements.txt`

- [ ] **Step 3: Create the schema file with its first table**

`sql/001_unifi_schema.sql`:

```sql
-- Canonical DDL for the Unifi scraper's Neon tables.
--
-- THIS FILE IS THE SOURCE OF TRUTH. The portal repo
-- (wifibizz_bill_generator) carries a copy as a hand-written Prisma
-- migration registered with `prisma migrate resolve --applied`.
--
-- Every object here is prefixed `unifi_`. The target schema already
-- contains the portal's own `orders` and `order_status_events` tables,
-- and Postgres scopes trigger functions to the schema, not the table.
--
-- Apply with:  psql "$DATABASE_URL" -f sql/001_unifi_schema.sql

-- One Unifi sales channel: a rover (van), KFA counter, affiliate or
-- reseller. `fleet_label` is what people call it ("CAR 1", "JEM 3").
CREATE TABLE IF NOT EXISTS unifi_channels (
    channel_code     text PRIMARY KEY,
    channel_name     text NOT NULL,
    plate            text,
    fleet_label      text,
    channel_category text,
    channel_type     text,
    active           boolean NOT NULL DEFAULT true,
    -- Fallback chain enforced by the database, not by a TypeScript
    -- expression on one side and a Python one on the other.
    display_name     text GENERATED ALWAYS AS (
        coalesce(nullif(fleet_label, ''), nullif(plate, ''), channel_name)
    ) STORED,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 4: Write the fixture**

`tests/__init__.py` is empty. `tests/conftest.py`:

```python
import os
import pathlib
import uuid

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

SQL_FILE = pathlib.Path(__file__).resolve().parent.parent / "sql" / "001_unifi_schema.sql"

ADMIN_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)


def _with_dbname(url: str, dbname: str) -> str:
    parts = conninfo_to_dict(url)
    parts["dbname"] = dbname
    return make_conninfo(**parts)


@pytest.fixture
def db_url():
    """A throwaway database, dropped on teardown. Yields its URL.

    Separate from `db` so that tests which need to hand a connection
    string to neon_writer get the same database the `db` fixture is
    inspecting.
    """
    name = f"unifi_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _with_dbname(ADMIN_URL, name)
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def db(db_url):
    """An open connection to a fresh database with the schema applied."""
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute(SQL_FILE.read_text())
        yield conn
```

- [ ] **Step 5: Write the failing tests**

`tests/test_schema.py`. The second test is acceptance criterion 3 from the parent spec, including the three channel codes whose sheet rows have blank name fields:

```python
import pytest


def test_fixture_builds_the_schema(db):
    row = db.execute(
        "SELECT to_regclass('public.unifi_channels')"
    ).fetchone()
    assert row[0] is not None, "unifi_channels was not created"


@pytest.mark.parametrize(
    "code, name, plate, fleet_label, expected",
    [
        # fleet label wins when present
        ("RV10551", "Rover 10551", "WXY 1234", "CAR 1", "CAR 1"),
        # falls back to plate when fleet label is blank
        ("RV10552", "Rover 10552", "WXY 5678", "", "WXY 5678"),
        # falls back to plate when fleet label is NULL
        ("RV10553", "Rover 10553", "WXY 9012", None, "WXY 9012"),
        # the sheet's known blank rows fall all the way through to
        # channel name -- never to an empty string
        ("RV10558", "Rover 10558", "", "", "Rover 10558"),
        ("AF10111", "Affiliate 10111", None, None, "Affiliate 10111"),
        ("RAGT10896", "Reseller 10896", "", None, "Reseller 10896"),
    ],
)
def test_display_name_fallback_chain(db, code, name, plate, fleet_label, expected):
    db.execute(
        "INSERT INTO unifi_channels (channel_code, channel_name, plate, fleet_label)"
        " VALUES (%s, %s, %s, %s)",
        (code, name, plate, fleet_label),
    )
    got = db.execute(
        "SELECT display_name FROM unifi_channels WHERE channel_code = %s", (code,)
    ).fetchone()[0]
    assert got == expected


def test_display_name_is_not_writable(db):
    db.execute(
        "INSERT INTO unifi_channels (channel_code, channel_name) VALUES ('RV1', 'One')"
    )
    with pytest.raises(psycopg_errors_GeneratedAlways):
        db.execute("UPDATE unifi_channels SET display_name = 'nope' WHERE channel_code = 'RV1'")
```

Add this import at the top of the file so the last test has its exception type:

```python
from psycopg.errors import GeneratedAlways as psycopg_errors_GeneratedAlways
```

- [ ] **Step 6: Run the tests**

Run: `pytest tests/test_schema.py -v`
Expected: all PASS. If `test_fixture_builds_the_schema` errors on connection, the Docker container is not up or `TEST_DATABASE_URL` is wrong — fix that before continuing, since every later task depends on this fixture.

- [ ] **Step 7: Add the env example and ignore the test artefacts**

`.env.example`:

```
# Google Sheets (existing)
GOOGLE_SHEETS_SPREADSHEET_ID=

# Allow the Gmail OTP reader to open a browser (existing)
GMAIL_ALLOW_BROWSER=

# Neon -- the scraper's OWN role, granted only on unifi_* tables.
# NOT the portal's connection string.
# Project wifibizz_bill_generator / branch production / database neondb
DATABASE_URL=postgresql://unifi_scraper:PASSWORD@HOST.neon.tech/neondb?sslmode=require

# Postgres used by the test suite. Defaults to a local Docker instance;
# set this to a scratch Neon branch if you have no Docker.
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
```

Append to `.gitignore`:

```
.pytest_cache/
```

- [ ] **Step 8: Commit**

```bash
git add requirements.txt .gitignore .env.example sql/001_unifi_schema.sql tests/__init__.py tests/conftest.py tests/test_schema.py
git commit -m "Add unifi_channels, the test fixture, and the Neon dependency

Records the pg_proc/pg_trigger audit of the production schema in this
message so the collision assumption is traceable:

<paste the output of Step 1c here>"
```

---

### Task 2: The orders table

**Files:**
- Modify: `sql/001_unifi_schema.sql` (append)
- Modify: `tests/test_schema.py` (append)

**Interfaces:**
- Consumes: the `db` fixture from Task 1.
- Produces: table `unifi_orders`, primary key `order_number`, with four indexes. Later tasks insert into it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_schema.py`:

```python
def test_orders_table_exists_with_expected_columns(db):
    cols = {
        r[0]: r[1]
        for r in db.execute(
            "SELECT column_name, data_type FROM information_schema.columns"
            " WHERE table_name = 'unifi_orders'"
        ).fetchall()
    }
    assert cols["order_number"] == "text"
    assert cols["created_date"] == "timestamp with time zone"
    assert cols["status_scrape_date"] == "timestamp with time zone"
    assert cols["raw"] == "jsonb"
    assert cols["last_synced"] == "timestamp with time zone"


def test_orders_indexes_exist(db):
    names = {
        r[0]
        for r in db.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'unifi_orders'"
        ).fetchall()
    }
    assert "unifi_orders_updated_date_idx" in names
    assert "unifi_orders_org_code_idx" in names
    assert "unifi_orders_order_status_idx" in names
    assert "unifi_orders_status_idx" in names


def test_org_code_is_not_a_foreign_key(db):
    # A rover appears in the Unifi portal before anyone adds it to the
    # fleet list. With an FK that order fails to insert and the data is
    # lost; without one it lands and shows up in the Unmapped panel.
    db.execute(
        "INSERT INTO unifi_orders (order_number, org_code) VALUES ('ORD1', 'RV99999')"
    )
    got = db.execute(
        "SELECT org_code FROM unifi_orders WHERE order_number = 'ORD1'"
    ).fetchone()[0]
    assert got == "RV99999"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_schema.py -v -k orders`
Expected: FAIL — `KeyError: 'order_number'` and empty index set, because the table does not exist.

- [ ] **Step 3: Append the table to the schema file**

```sql
-- Scraped from the Unifi dealer portal. `org_code` is deliberately NOT a
-- foreign key to unifi_channels: a new rover appears in the portal
-- before anyone adds it to the fleet list, and an FK would drop that
-- order on the floor instead of surfacing it as work to do.
CREATE TABLE IF NOT EXISTS unifi_orders (
    order_number       text PRIMARY KEY,
    event_type         text,
    order_status       text,
    created_date       timestamptz,
    updated_date       timestamptz,
    org_code           text,
    organization_name  text,
    customer_name      text,
    company_name       text,
    email              text,
    phone_number       text,
    appointment_date   timestamptz,
    address            text,
    package            text,
    device             text,
    ic_number          text,
    creator            text,
    cust_id            text,
    -- installation status, from the subscriber API
    status             text,
    status_latest_date timestamptz,
    -- when we last VERIFIED the status, as opposed to when it changed
    status_scrape_date timestamptz,
    last_synced        timestamptz NOT NULL DEFAULT now(),
    raw                jsonb
);

CREATE INDEX IF NOT EXISTS unifi_orders_updated_date_idx
    ON unifi_orders (updated_date DESC);
CREATE INDEX IF NOT EXISTS unifi_orders_org_code_idx
    ON unifi_orders (org_code);
CREATE INDEX IF NOT EXISTS unifi_orders_order_status_idx
    ON unifi_orders (order_status);
CREATE INDEX IF NOT EXISTS unifi_orders_status_idx
    ON unifi_orders (status);
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_schema.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sql/001_unifi_schema.sql tests/test_schema.py
git commit -m "Add unifi_orders with its four indexes"
```

---

### Task 3: The events table and the status trigger

This is the heart of the change. Acceptance criterion 2 of the parent spec lives or dies on the guard.

**Files:**
- Modify: `sql/001_unifi_schema.sql` (append)
- Create: `tests/test_trigger.py`

**Interfaces:**
- Consumes: `unifi_orders` from Task 2, the `db` fixture from Task 1.
- Produces: table `unifi_order_status_events`; function `unifi_log_order_status_change()`; trigger `unifi_orders_status_change`. Events pick up a run id from the session setting `unifi.scrape_run_id` when one is set — Task 10 sets it.

- [ ] **Step 1: Write the failing tests**

`tests/test_trigger.py`:

```python
def _events(db, order_number="ORD1"):
    return db.execute(
        "SELECT prev_order_status, order_status, prev_status, status, cust_id"
        "  FROM unifi_order_status_events"
        " WHERE order_number = %s ORDER BY id",
        (order_number,),
    ).fetchall()


def test_insert_writes_one_event_with_null_previous(db):
    # prev_* NULL is what the portal renders as "State at migration".
    db.execute(
        "INSERT INTO unifi_orders (order_number, order_status, status)"
        " VALUES ('ORD1', 'In Progress', 'Pending')"
    )
    rows = _events(db)
    assert len(rows) == 1
    assert rows[0] == (None, "In Progress", None, "Pending", None)


def test_insert_then_status_update_produces_exactly_two_events(db):
    # Acceptance criterion 2, first half.
    db.execute(
        "INSERT INTO unifi_orders (order_number, order_status, status)"
        " VALUES ('ORD1', 'In Progress', 'Pending')"
    )
    db.execute("UPDATE unifi_orders SET status = 'Active' WHERE order_number = 'ORD1'")
    rows = _events(db)
    assert len(rows) == 2
    assert rows[1][2] == "Pending"
    assert rows[1][3] == "Active"


def test_repeating_the_same_update_produces_no_event(db):
    # Acceptance criterion 2, second half.
    db.execute(
        "INSERT INTO unifi_orders (order_number, order_status, status)"
        " VALUES ('ORD1', 'In Progress', 'Pending')"
    )
    db.execute("UPDATE unifi_orders SET status = 'Active' WHERE order_number = 'ORD1'")
    before = len(_events(db))
    db.execute("UPDATE unifi_orders SET status = 'Active' WHERE order_number = 'ORD1'")
    db.execute("UPDATE unifi_orders SET status = 'Active' WHERE order_number = 'ORD1'")
    assert len(_events(db)) == before


def test_scrape_date_moving_alone_produces_no_event(db):
    # This is the one that matters nightly: check_status stamps
    # status_scrape_date and last_synced on EVERY order it verifies,
    # whether or not anything changed. If those were in the guard, one
    # quiet night would write one event per order.
    db.execute(
        "INSERT INTO unifi_orders (order_number, order_status, status)"
        " VALUES ('ORD1', 'In Progress', 'Active')"
    )
    before = len(_events(db))
    db.execute(
        "UPDATE unifi_orders"
        "   SET status_scrape_date = now(), last_synced = now()"
        " WHERE order_number = 'ORD1'"
    )
    assert len(_events(db)) == before


def test_cust_id_change_is_an_event(db):
    # check_custid.py rewrites 10XXX cust ids nightly; a swap is a real
    # observation and the portal shows it beside the status transition.
    db.execute(
        "INSERT INTO unifi_orders (order_number, status, cust_id)"
        " VALUES ('ORD1', 'Active', '10555')"
    )
    db.execute("UPDATE unifi_orders SET cust_id = '20999' WHERE order_number = 'ORD1'")
    rows = _events(db)
    assert len(rows) == 2
    assert rows[1][4] == "20999"


def test_order_status_change_is_an_event(db):
    db.execute(
        "INSERT INTO unifi_orders (order_number, order_status) VALUES ('ORD1', 'In Progress')"
    )
    db.execute(
        "UPDATE unifi_orders SET order_status = 'Completed' WHERE order_number = 'ORD1'"
    )
    rows = _events(db)
    assert len(rows) == 2
    assert rows[1][0] == "In Progress"
    assert rows[1][1] == "Completed"


def test_unrelated_column_change_produces_no_event(db):
    db.execute(
        "INSERT INTO unifi_orders (order_number, address) VALUES ('ORD1', 'Old address')"
    )
    before = len(_events(db))
    db.execute("UPDATE unifi_orders SET address = 'New address' WHERE order_number = 'ORD1'")
    assert len(_events(db)) == before


def test_event_picks_up_the_run_id_when_one_is_set(db):
    db.execute("SELECT set_config('unifi.scrape_run_id', '42', false)")
    db.execute("INSERT INTO unifi_orders (order_number, status) VALUES ('ORD1', 'Active')")
    got = db.execute(
        "SELECT scrape_run_id FROM unifi_order_status_events WHERE order_number = 'ORD1'"
    ).fetchone()[0]
    assert got == 42


def test_event_run_id_is_null_when_unset(db):
    db.execute("INSERT INTO unifi_orders (order_number, status) VALUES ('ORD1', 'Active')")
    got = db.execute(
        "SELECT scrape_run_id FROM unifi_order_status_events WHERE order_number = 'ORD1'"
    ).fetchone()[0]
    assert got is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_trigger.py -v`
Expected: FAIL — `relation "unifi_order_status_events" does not exist`.

- [ ] **Step 3: Append the table, function and trigger**

```sql
-- Append-only. One row per OBSERVED CHANGE, never one per check.
CREATE TABLE IF NOT EXISTS unifi_order_status_events (
    id                 bigserial PRIMARY KEY,
    order_number       text NOT NULL,
    order_status       text,
    status             text,
    status_latest_date timestamptz,
    cust_id            text,
    prev_order_status  text,
    prev_status        text,
    changed_at         timestamptz NOT NULL DEFAULT now(),
    scrape_run_id      bigint
);

CREATE INDEX IF NOT EXISTS unifi_order_status_events_order_changed_idx
    ON unifi_order_status_events (order_number, changed_at DESC);
CREATE INDEX IF NOT EXISTS unifi_order_status_events_changed_idx
    ON unifi_order_status_events (changed_at DESC);

-- NAME MATTERS. The portal owns an `order_status_events` table in this
-- same schema and very likely a `log_order_status_change()` behind it.
-- Trigger functions are schema-scoped, and CREATE OR REPLACE FUNCTION
-- does not error on a name clash -- it replaces. Hence the prefix.
CREATE OR REPLACE FUNCTION unifi_log_order_status_change()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    -- Exactly three columns are compared. status_scrape_date and
    -- last_synced move on every single check and must stay out, or a
    -- quiet night writes one event per order.
    IF TG_OP = 'UPDATE' AND NOT (
           NEW.order_status IS DISTINCT FROM OLD.order_status
        OR NEW.status       IS DISTINCT FROM OLD.status
        OR NEW.cust_id      IS DISTINCT FROM OLD.cust_id
    ) THEN
        RETURN NULL;
    END IF;

    INSERT INTO unifi_order_status_events (
        order_number, order_status, status, status_latest_date, cust_id,
        prev_order_status, prev_status, scrape_run_id
    ) VALUES (
        NEW.order_number,
        NEW.order_status,
        NEW.status,
        NEW.status_latest_date,
        NEW.cust_id,
        CASE WHEN TG_OP = 'UPDATE' THEN OLD.order_status END,
        CASE WHEN TG_OP = 'UPDATE' THEN OLD.status END,
        nullif(current_setting('unifi.scrape_run_id', true), '')::bigint
    );

    RETURN NULL;
END;
$fn$;

DROP TRIGGER IF EXISTS unifi_orders_status_change ON unifi_orders;
CREATE TRIGGER unifi_orders_status_change
    AFTER INSERT OR UPDATE ON unifi_orders
    FOR EACH ROW
    EXECUTE FUNCTION unifi_log_order_status_change();
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_trigger.py -v`
Expected: all PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add sql/001_unifi_schema.sql tests/test_trigger.py
git commit -m "Add the status-change trigger, guarded on three columns

status_scrape_date and last_synced are deliberately outside the guard.
check_status stamps them on every order it verifies, so including them
would write one event per order per night and make the timeline
worthless."
```

---

### Task 4: The scrape-runs table

**Files:**
- Modify: `sql/001_unifi_schema.sql` (append)
- Modify: `tests/test_schema.py` (append)

**Interfaces:**
- Consumes: the `db` fixture.
- Produces: table `unifi_scrape_runs` with `bigserial` id. Task 10 writes to it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_schema.py`:

```python
def test_scrape_runs_round_trips(db):
    run_id = db.execute(
        "INSERT INTO unifi_scrape_runs (month_text, year, scrape_mode, triggered_by, status)"
        " VALUES ('Sep', 2026, 'incremental', 'cron', 'running') RETURNING id"
    ).fetchone()[0]
    assert isinstance(run_id, int)

    db.execute(
        "UPDATE unifi_scrape_runs"
        "   SET status = 'done', finished_at = now(), orders_processed = 412,"
        "       successful = 409, skipped = 0, failed = 3"
        " WHERE id = %s",
        (run_id,),
    )
    row = db.execute(
        "SELECT status, orders_processed, failed, started_at IS NOT NULL"
        "  FROM unifi_scrape_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert row == ("done", 412, 3, True)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_schema.py -v -k scrape_runs`
Expected: FAIL — `relation "unifi_scrape_runs" does not exist`.

- [ ] **Step 3: Append the table**

```sql
CREATE TABLE IF NOT EXISTS unifi_scrape_runs (
    id               bigserial PRIMARY KEY,
    job_id           text,
    month_text       text,
    year             integer,
    scrape_mode      text,
    triggered_by     text,          -- "cron" | "admin"
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    status           text,          -- running | done | error
    orders_processed integer,
    successful       integer,
    skipped          integer,
    failed           integer,
    error            text
);

CREATE INDEX IF NOT EXISTS unifi_scrape_runs_started_idx
    ON unifi_scrape_runs (started_at DESC);
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_schema.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sql/001_unifi_schema.sql tests/test_schema.py
git commit -m "Add unifi_scrape_runs"
```

---

### Task 5: The four views

**Files:**
- Modify: `sql/001_unifi_schema.sql` (append)
- Create: `tests/test_views.py`

**Interfaces:**
- Consumes: all four tables.
- Produces: views `unifi_order_status_timeline`, `unifi_monthly_stats`, `unifi_monthly_channel_breakdown`, `unifi_unmapped_channels`. The portal reads these via `$queryRaw`.

- [ ] **Step 1: Write the failing tests**

`tests/test_views.py`. The `monthly_stats` test is acceptance criterion 4, the `unmapped` test is criterion 6:

```python
import pytest


@pytest.fixture
def seeded(db):
    db.execute(
        "INSERT INTO unifi_channels (channel_code, channel_name, fleet_label)"
        " VALUES ('RV10551', 'Rover 10551', 'CAR 1')"
    )
    rows = [
        # (order_number, org_code, organization_name, order_status)
        ("O1", "RV10551", "Rover 10551", "Completed"),
        ("O2", "RV10551", "Rover 10551", "completed"),   # case-insensitive
        ("O3", "RV10551", "Rover 10551", "Cancelled"),
        ("O4", "RV10551", "Rover 10551", "Order Voided"),  # substring
        ("O5", "RV10551", "Rover 10551", "Activation Failed"),  # substring
        ("O6", "RV10551", "Rover 10551", "In Progress"),
        ("O7", "RV10551", "Rover 10551", None),          # NULL counts as other
        # an org code nobody has added to the fleet list
        ("O8", "RV99999", "Rover 99999", "In Progress"),
    ]
    for order_number, org, org_name, order_status in rows:
        db.execute(
            "INSERT INTO unifi_orders"
            " (order_number, org_code, organization_name, order_status, created_date)"
            " VALUES (%s, %s, %s, %s, '2026-09-15 10:00+08')",
            (order_number, org, org_name, order_status),
        )
    return db


def test_all_four_views_exist(db):
    names = {
        r[0]
        for r in db.execute(
            "SELECT table_name FROM information_schema.views WHERE table_schema = 'public'"
        ).fetchall()
    }
    assert {
        "unifi_order_status_timeline",
        "unifi_monthly_stats",
        "unifi_monthly_channel_breakdown",
        "unifi_unmapped_channels",
    } <= names


def test_monthly_stats_matches_the_telegram_definitions(seeded):
    row = seeded.execute(
        "SELECT total, completed, cancelled, other FROM unifi_monthly_stats"
    ).fetchone()
    total, completed, cancelled, other = row
    assert total == 8
    assert completed == 2            # 'Completed' and 'completed', exact match only
    assert cancelled == 3            # Cancelled, Order Voided, Activation Failed
    assert other == 3                # In Progress x2, NULL x1
    assert completed + cancelled + other == total


def test_monthly_stats_does_not_count_in_progress_as_completed(seeded):
    # 'completed' is an EXACT match, so a status merely containing the
    # word must not be counted.
    seeded.execute(
        "INSERT INTO unifi_orders (order_number, order_status, created_date)"
        " VALUES ('O9', 'Not Completed', '2026-09-15 10:00+08')"
    )
    completed = seeded.execute("SELECT completed FROM unifi_monthly_stats").fetchone()[0]
    assert completed == 2


def test_unmapped_channels_surfaces_org_codes_with_no_channel_row(seeded):
    rows = seeded.execute(
        "SELECT org_code, organization_name, order_count FROM unifi_unmapped_channels"
    ).fetchall()
    assert rows == [("RV99999", "Rover 99999", 1)]


def test_channel_breakdown_uses_display_name(seeded):
    row = seeded.execute(
        "SELECT channel_display_name, total FROM unifi_monthly_channel_breakdown"
        " WHERE org_code = 'RV10551'"
    ).fetchone()
    assert row == ("CAR 1", 7)


def test_channel_breakdown_falls_back_for_unmapped_codes(seeded):
    row = seeded.execute(
        "SELECT channel_display_name FROM unifi_monthly_channel_breakdown"
        " WHERE org_code = 'RV99999'"
    ).fetchone()
    assert row[0] == "Rover 99999"


def test_timeline_reports_how_long_the_previous_state_held(db):
    db.execute(
        "INSERT INTO unifi_orders (order_number, status) VALUES ('O1', 'Pending')"
    )
    db.execute(
        "UPDATE unifi_order_status_events SET changed_at = '2026-09-01 00:00+08'"
        " WHERE order_number = 'O1'"
    )
    db.execute("UPDATE unifi_orders SET status = 'Active' WHERE order_number = 'O1'")
    db.execute(
        "UPDATE unifi_order_status_events SET changed_at = '2026-09-04 00:00+08'"
        " WHERE order_number = 'O1' AND status = 'Active'"
    )
    rows = db.execute(
        "SELECT status, previous_held_for FROM unifi_order_status_timeline"
        " WHERE order_number = 'O1' ORDER BY changed_at"
    ).fetchall()
    assert rows[0][1] is None            # nothing preceded the first event
    assert rows[1][1].days == 3


def test_timeline_carries_the_channel_display_name(seeded):
    seeded.execute("UPDATE unifi_orders SET status = 'Active' WHERE order_number = 'O1'")
    row = seeded.execute(
        "SELECT channel_display_name FROM unifi_order_status_timeline"
        " WHERE order_number = 'O1' ORDER BY changed_at DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "CAR 1"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_views.py -v`
Expected: FAIL — the views do not exist.

- [ ] **Step 3: Append the views**

```sql
-- One row per observed transition, with the channel label resolved and
-- how long each state held. `previous_held_for` is what the portal's
-- feed prints beside a transition; `held_for` is NULL while a state is
-- still the current one.
CREATE OR REPLACE VIEW unifi_order_status_timeline AS
SELECT
    e.id,
    e.order_number,
    e.changed_at,
    e.prev_order_status,
    e.order_status,
    e.prev_status,
    e.status,
    e.status_latest_date,
    e.cust_id,
    e.scrape_run_id,
    o.org_code,
    coalesce(c.display_name, o.organization_name, o.org_code) AS channel_display_name,
    e.changed_at - lag(e.changed_at) OVER w  AS previous_held_for,
    lead(e.changed_at) OVER w - e.changed_at AS held_for
FROM unifi_order_status_events e
LEFT JOIN unifi_orders   o ON o.order_number  = e.order_number
LEFT JOIN unifi_channels c ON c.channel_code  = o.org_code
WINDOW w AS (PARTITION BY e.order_number ORDER BY e.changed_at, e.id);

-- Definitions copied verbatim from the spec so the portal's stat tiles
-- and the Telegram message can never disagree:
--   completed = lower(order_status) = 'completed'   (exact)
--   cancelled = order_status ~* 'cancel|void|failed' (substring)
--   other     = everything else, INCLUDING NULL
CREATE OR REPLACE VIEW unifi_monthly_stats AS
SELECT
    date_trunc('month', created_date) AS month,
    count(*) AS total,
    count(*) FILTER (
        WHERE lower(coalesce(order_status, '')) = 'completed'
    ) AS completed,
    count(*) FILTER (
        WHERE coalesce(order_status, '') ~* '(cancel|void|failed)'
    ) AS cancelled,
    count(*) FILTER (
        WHERE lower(coalesce(order_status, '')) <> 'completed'
          AND coalesce(order_status, '') !~* '(cancel|void|failed)'
    ) AS other
FROM unifi_orders
WHERE created_date IS NOT NULL
GROUP BY 1;

CREATE OR REPLACE VIEW unifi_monthly_channel_breakdown AS
SELECT
    date_trunc('month', o.created_date) AS month,
    o.org_code,
    coalesce(c.display_name, o.organization_name, o.org_code) AS channel_display_name,
    count(*) AS total,
    count(*) FILTER (
        WHERE lower(coalesce(o.order_status, '')) = 'completed'
    ) AS completed,
    count(*) FILTER (
        WHERE coalesce(o.order_status, '') ~* '(cancel|void|failed)'
    ) AS cancelled
FROM unifi_orders o
LEFT JOIN unifi_channels c ON c.channel_code = o.org_code
WHERE o.created_date IS NOT NULL
GROUP BY 1, 2, 3;

-- Org codes that appear on orders but have no channel row. This is the
-- queue that is invisible today: those orders print in Telegram as
-- "RV10xxx | <Organization Name>" and nobody notices.
CREATE OR REPLACE VIEW unifi_unmapped_channels AS
SELECT
    o.org_code,
    max(o.organization_name) AS organization_name,
    count(*)                 AS order_count,
    max(o.created_date)      AS latest_order_date
FROM unifi_orders o
LEFT JOIN unifi_channels c ON c.channel_code = o.org_code
WHERE o.org_code IS NOT NULL
  AND o.org_code <> ''
  AND c.channel_code IS NULL
GROUP BY o.org_code;
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_views.py -v`
Expected: all PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add sql/001_unifi_schema.sql tests/test_views.py
git commit -m "Add the four Unifi views

monthly_stats uses the Telegram message's own definitions so the
portal's tiles cannot drift from it: completed is an exact match,
cancelled is a substring match, and NULL order_status counts as other."
```

---

### Task 6: Grants and the checksum header

**Files:**
- Modify: `sql/001_unifi_schema.sql` (append grants, prepend header)
- Create: `sql/checksum.py`
- Create: `tests/test_checksum.py`

**Interfaces:**
- Consumes: all four tables.
- Produces: role `unifi_scraper` with minimal grants; `sql/checksum.py` exposing `compute(path) -> str` and `header_value(path) -> str | None`, plus a `--write` CLI.

- [ ] **Step 1: Write the failing test**

`tests/test_checksum.py`:

```python
import pathlib
import subprocess
import sys

from sql.checksum import compute, header_value

SQL_FILE = pathlib.Path(__file__).resolve().parent.parent / "sql" / "001_unifi_schema.sql"


def test_header_matches_the_body():
    # If this fails, someone edited the DDL without re-running
    # `python -m sql.checksum --write`. The portal's Prisma migration
    # carries the same value; a stale one there means silent drift.
    assert header_value(SQL_FILE) == compute(SQL_FILE), (
        "schema-checksum header is stale -- run: python -m sql.checksum --write"
    )


def test_write_is_idempotent(tmp_path):
    f = tmp_path / "x.sql"
    f.write_text("-- schema-checksum: pending\nSELECT 1;\n")
    subprocess.run([sys.executable, "-m", "sql.checksum", "--write", str(f)], check=True)
    first = f.read_text()
    subprocess.run([sys.executable, "-m", "sql.checksum", "--write", str(f)], check=True)
    assert f.read_text() == first


def test_changing_the_body_changes_the_checksum(tmp_path):
    f = tmp_path / "x.sql"
    f.write_text("-- schema-checksum: pending\nSELECT 1;\n")
    before = compute(f)
    f.write_text("-- schema-checksum: pending\nSELECT 2;\n")
    assert compute(f) != before
```

Add a grants test to `tests/test_schema.py`:

```python
def test_scraper_role_has_minimal_grants(db):
    def privs(table):
        return {
            r[0]
            for r in db.execute(
                "SELECT privilege_type FROM information_schema.table_privileges"
                " WHERE grantee = 'unifi_scraper' AND table_name = %s",
                (table,),
            ).fetchall()
        }

    assert privs("unifi_orders") == {"SELECT", "INSERT", "UPDATE"}
    # INSERT here is NOT optional: a trigger function runs with the
    # privileges of the role that fired it, not the table owner, so
    # without this every scraper write fails.
    assert privs("unifi_order_status_events") == {"SELECT", "INSERT"}
    assert privs("unifi_scrape_runs") == {"SELECT", "INSERT", "UPDATE"}
    # The portal owns the channel list; the scraper only reads it.
    assert privs("unifi_channels") == {"SELECT"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_checksum.py tests/test_schema.py -v -k "checksum or grants or role"`
Expected: FAIL — `ModuleNotFoundError: No module named 'sql'` and an empty privilege set.

- [ ] **Step 3: Write the checksum module**

`sql/__init__.py` (empty file), and `sql/checksum.py`:

```python
"""Checksum the canonical schema file so the portal's copy cannot drift.

The first line of the SQL file is `-- schema-checksum: <sha256>`, taken
over every byte AFTER that line. The portal repo's hand-written Prisma
migration carries the same value.

Usage:
  python -m sql.checksum                 # print expected vs actual
  python -m sql.checksum --write         # rewrite the header
"""

import hashlib
import pathlib
import sys

HEADER_PREFIX = "-- schema-checksum:"
DEFAULT_PATH = pathlib.Path(__file__).resolve().parent / "001_unifi_schema.sql"


def _split(path):
    lines = pathlib.Path(path).read_text().splitlines(keepends=True)
    if lines and lines[0].startswith(HEADER_PREFIX):
        return lines[0], "".join(lines[1:])
    return None, "".join(lines)


def compute(path=DEFAULT_PATH) -> str:
    _, body = _split(path)
    return hashlib.sha256(body.encode()).hexdigest()


def header_value(path=DEFAULT_PATH):
    header, _ = _split(path)
    if header is None:
        return None
    return header[len(HEADER_PREFIX):].strip()


def write(path=DEFAULT_PATH) -> str:
    path = pathlib.Path(path)
    _, body = _split(path)
    digest = hashlib.sha256(body.encode()).hexdigest()
    path.write_text(f"{HEADER_PREFIX} {digest}\n{body}")
    return digest


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--write"]
    target = pathlib.Path(args[0]) if args else DEFAULT_PATH
    if "--write" in sys.argv[1:]:
        print(f"{target}: {write(target)}")
    else:
        expected, actual = compute(target), header_value(target)
        print(f"expected: {expected}\nheader:   {actual}")
        sys.exit(0 if expected == actual else 1)
```

- [ ] **Step 4: Append the grants to the schema file**

```sql
-- The scraper connects as its own role, not as the portal's. It runs on
-- a droplet with credentials on disk; a compromise there should not
-- reach the portal's own tables.
--
-- Roles are cluster-wide, not per-database, so this is guarded for the
-- test suite, which builds many databases in one cluster.
DO $role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'unifi_scraper') THEN
        CREATE ROLE unifi_scraper LOGIN;
    END IF;
END
$role$;

GRANT USAGE ON SCHEMA public TO unifi_scraper;

GRANT SELECT, INSERT, UPDATE ON unifi_orders     TO unifi_scraper;
GRANT SELECT, INSERT, UPDATE ON unifi_scrape_runs TO unifi_scraper;
GRANT SELECT                  ON unifi_channels   TO unifi_scraper;

-- A trigger function runs with the privileges of the role that caused
-- it to fire, NOT the table owner. Without INSERT here, every write the
-- scraper makes to unifi_orders fails. The alternative is SECURITY
-- DEFINER, which is more privilege than a row-level audit trigger
-- should carry.
GRANT SELECT, INSERT ON unifi_order_status_events TO unifi_scraper;

GRANT USAGE ON SEQUENCE unifi_order_status_events_id_seq TO unifi_scraper;
GRANT USAGE ON SEQUENCE unifi_scrape_runs_id_seq         TO unifi_scraper;
```

- [ ] **Step 5: Write the checksum header**

Run: `python -m sql.checksum --write`

This prepends the `-- schema-checksum: <sha256>` line. Re-run it after **any** future edit to the SQL file — `tests/test_checksum.py` fails otherwise.

- [ ] **Step 6: Run the whole suite**

Run: `pytest -v`
Expected: all PASS.

- [ ] **Step 7: Set the role's password on the Neon dev branch**

Not in the SQL file — it is a secret. In the Neon SQL Editor on the dev branch:

```sql
ALTER ROLE unifi_scraper WITH PASSWORD 'generate-a-strong-one';
```

Put the resulting connection string in `.env` as `DATABASE_URL`. Confirm `.env` is gitignored (it is, line 2 of `.gitignore`).

- [ ] **Step 8: Commit**

```bash
git add sql/__init__.py sql/checksum.py sql/001_unifi_schema.sql tests/test_checksum.py tests/test_schema.py
git commit -m "Add the unifi_scraper role, its grants, and a checksum header

The events-table INSERT grant is load-bearing: trigger functions run as
the invoking role, so without it every scraper write fails. Using
SECURITY DEFINER instead would hand a row-level audit trigger more
privilege than it needs."
```

---

### Task 7: The coercion layer

Pure functions, no database. This is where every quirk of the sheet's string formats gets handled exactly once.

**Files:**
- Create: `neon_writer.py`
- Create: `tests/test_coercion.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `neon_writer.coerce_row(row: dict) -> dict` mapping Sheet headers to typed column values; `neon_writer.parse_dt(value) -> datetime | None`; `neon_writer.text(value) -> str | None`; `neon_writer.status_text(value) -> str | None`; `neon_writer.LOCAL_TZ`. `coerce_row` returns `raw` as a plain dict — Task 8 wraps it in `Jsonb` at bind time so this module stays testable without a database.

- [ ] **Step 1: Write the failing tests**

The date formats below are not guesses — they are what the existing code emits. `format_datetime` in `scrape_orders.py:28` and `standardize_date` in `date_utils.py` both produce `"%d %b %Y %H:%M"`. `_extract_status_date` in `check_status.py:531` produces `"%d %b %Y"` with **no time**, and falls through returning the raw `"%Y/%m/%d %H:%M:%S"` when the API gives it something unexpected. `Last Synced` is written as `"'" + "%Y-%m-%d %H:%M:%S"`, apostrophe included.

`tests/test_coercion.py`:

```python
from datetime import datetime

import pytest

from neon_writer import LOCAL_TZ, coerce_row, parse_dt, status_text, text


@pytest.mark.parametrize(
    "raw, expected",
    [
        # what format_datetime() and standardize_date() emit
        ("22 Oct 2025 09:30", datetime(2025, 10, 22, 9, 30)),
        ("01 Dec 2025 13:00:00", datetime(2025, 12, 1, 13, 0, 0)),
        # what _extract_status_date() emits -- DATE ONLY, no time
        ("22 Oct 2025", datetime(2025, 10, 22, 0, 0)),
        # what the Last Synced column holds, apostrophe and all
        ("'2026-09-20 14:30:00", datetime(2026, 9, 20, 14, 30, 0)),
        ("2026-09-20 14:30:00", datetime(2026, 9, 20, 14, 30, 0)),
        # the API format, when _extract_status_date could not parse it
        ("2025/10/22 09:30:00", datetime(2025, 10, 22, 9, 30, 0)),
        # the raw dealer-portal format
        ("20251022093000", datetime(2025, 10, 22, 9, 30, 0)),
    ],
)
def test_parse_dt_handles_every_format_the_scraper_emits(raw, expected):
    got = parse_dt(raw)
    assert got == expected.replace(tzinfo=LOCAL_TZ)


def test_parse_dt_attaches_the_local_timezone():
    # Every date the scraper writes is naive Kuala Lumpur time. Storing
    # it without a zone would shift every timestamp by 8 hours.
    got = parse_dt("22 Oct 2025 09:30")
    assert got.tzinfo is not None
    assert got.utcoffset().total_seconds() == 8 * 3600


@pytest.mark.parametrize("raw", ["", "   ", None, "not a date", "N/A", "-"])
def test_parse_dt_returns_none_rather_than_raising(raw):
    # A garbage cell must not kill a scrape.
    assert parse_dt(raw) is None


def test_text_strips_the_sheet_apostrophe():
    # Order numbers are written as "'12345" so Sheets treats them as
    # text instead of rendering 1.2345e4.
    assert text("'1234567890123") == "1234567890123"


@pytest.mark.parametrize("raw, expected", [("  x  ", "x"), ("", None), ("   ", None), (None, None)])
def test_text_blanks_become_none(raw, expected):
    assert text(raw) == expected


def test_status_text_maps_the_cancelled_sentinel_to_none():
    # "-" is the sheet's "cancelled, don't check" marker. It was never an
    # observed state, and storing it literally would fill the churn
    # matrix with transitions into "-".
    assert status_text("-") is None
    assert status_text("Active") == "Active"
    assert status_text("") is None


def test_coerce_row_maps_every_sheet_header():
    row = {
        "Order Number": "'1234567890",
        "Event Type": "New Install",
        "Order Status": "Completed",
        "Created Date": "22 Oct 2025 09:30",
        "Updated Date": "23 Oct 2025 11:00",
        "Org Code": "RV10551",
        "Organization Name": "Rover 10551",
        "Name": "Ali bin Abu",
        "Company Name": "",
        "Email": "ali@example.com",
        "Phone Number": "0123456789",
        "Appointment Date": "25 Oct 2025 14:00",
        "Address": "12 Jalan Satu",
        "Package": "UNI5G Postpaid 99",
        "Device": "Modem X",
        "IC Number": "900101015555 (MyKad)",
        "Creator": "Siti (S123)",
        "Last Synced": "'2026-09-20 14:30:00",
        "Cust ID": "10555",
        "Status": "Active",
        "Status Latest Date": "22 Oct 2025",
        "Status Scrape Date": "'2026-09-20 14:30:00",
    }
    got = coerce_row(row)

    assert got["order_number"] == "1234567890"
    assert got["customer_name"] == "Ali bin Abu"      # "Name" -> customer_name
    assert got["company_name"] is None                # blank -> NULL
    assert got["org_code"] == "RV10551"
    assert got["status"] == "Active"
    assert got["created_date"] == parse_dt("22 Oct 2025 09:30")
    assert got["status_latest_date"] == parse_dt("22 Oct 2025")
    assert got["raw"] == row                          # plain dict, wrapped later


def test_coerce_row_keeps_the_whole_row_in_raw():
    row = {"Order Number": "1", "Some Future Column": "value"}
    assert coerce_row(row)["raw"]["Some Future Column"] == "value"


def test_coerce_row_defaults_last_synced_to_now():
    got = coerce_row({"Order Number": "1"})
    assert got["last_synced"] is not None
    assert got["last_synced"].tzinfo is not None


def test_coerce_row_tolerates_missing_keys():
    # check_status and the backfill both hand over partial rows.
    got = coerce_row({"Order Number": "1"})
    assert got["order_number"] == "1"
    assert got["address"] is None
    assert got["created_date"] is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_coercion.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'neon_writer'`.

- [ ] **Step 3: Write the coercion layer**

`neon_writer.py`:

```python
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_coercion.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add neon_writer.py tests/test_coercion.py
git commit -m "Add the Sheet-to-Postgres coercion layer

Every date format here is one the existing code actually emits, not a
guess: format_datetime and standardize_date produce '%d %b %Y %H:%M',
_extract_status_date produces a date with no time, and Last Synced
carries the leading apostrophe that keeps Sheets from rendering order
numbers in scientific notation."
```

---

### Task 8: The connection pool and `upsert_orders`

**Files:**
- Modify: `neon_writer.py` (append)
- Create: `tests/test_neon_writer.py`

**Interfaces:**
- Consumes: `coerce_row` from Task 7; the schema from Tasks 2–6.
- Produces: `neon_writer.upsert_orders(rows: list[dict]) -> int`; `neon_writer.write_failure_count() -> int`; `neon_writer.reset_failures()`; `neon_writer.close()`. Reads `DATABASE_URL` from the environment; no-ops harmlessly when it is unset.

- [ ] **Step 1: Write the failing tests**

`tests/test_neon_writer.py`. The fixture repoints the module at the throwaway database:

```python
import pytest

import neon_writer


@pytest.fixture
def writer(db, db_url, monkeypatch):
    """Point neon_writer at the same throwaway database `db` inspects."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    neon_writer.close()
    neon_writer.reset_failures()
    yield neon_writer
    neon_writer.close()


def _row(order_number, **over):
    row = {
        "Order Number": order_number,
        "Order Status": "In Progress",
        "Created Date": "22 Oct 2025 09:30",
        "Updated Date": "22 Oct 2025 09:30",
        "Org Code": "RV10551",
        "Organization Name": "Rover 10551",
        "Name": "Ali bin Abu",
        "Address": "12 Jalan Satu",
        "Package": "UNI5G Postpaid 99",
        "Cust ID": "10555",
        "Last Synced": "'2026-09-20 14:30:00",
    }
    row.update(over)
    return row


def test_upsert_inserts_a_new_order(writer, db):
    assert writer.upsert_orders([_row("O1")]) == 1
    row = db.execute(
        "SELECT customer_name, org_code, package FROM unifi_orders WHERE order_number = 'O1'"
    ).fetchone()
    assert row == ("Ali bin Abu", "RV10551", "UNI5G Postpaid 99")


def test_upsert_updates_an_existing_order(writer, db):
    writer.upsert_orders([_row("O1")])
    writer.upsert_orders([_row("O1", **{"Order Status": "Completed"})])
    rows = db.execute("SELECT order_status FROM unifi_orders WHERE order_number = 'O1'").fetchall()
    assert rows == [("Completed",)]


def test_upsert_does_not_clobber_a_status_with_null(writer, db):
    # scrape_orders never checks installation status, so its rows carry
    # no "Status" key. Letting that overwrite would wipe what
    # check_status learned an hour earlier.
    writer.upsert_orders([_row("O1", **{"Status": "Active"})])
    writer.upsert_orders([_row("O1")])          # no Status key at all
    got = db.execute("SELECT status FROM unifi_orders WHERE order_number = 'O1'").fetchone()
    assert got[0] == "Active"


def test_upsert_stores_the_raw_row(writer, db):
    writer.upsert_orders([_row("O1", **{"Device": "Modem X"})])
    got = db.execute("SELECT raw->>'Device' FROM unifi_orders WHERE order_number = 'O1'").fetchone()
    assert got[0] == "Modem X"


def test_upsert_writes_many_rows_in_one_call(writer, db):
    assert writer.upsert_orders([_row(f"O{i}") for i in range(50)]) == 50
    assert db.execute("SELECT count(*) FROM unifi_orders").fetchone()[0] == 50


def test_upsert_skips_rows_with_no_order_number(writer, db):
    writer.upsert_orders([_row("O1"), _row(""), _row(None)])
    assert db.execute("SELECT count(*) FROM unifi_orders").fetchone()[0] == 1


def test_upsert_of_an_empty_list_is_a_no_op(writer):
    assert writer.upsert_orders([]) == 0
    assert writer.write_failure_count() == 0


def test_a_broken_connection_never_raises(monkeypatch):
    # The whole point: Google Sheets is authoritative, and a Neon outage
    # must not fail a scrape.
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/nope")
    monkeypatch.setenv("NEON_POOL_TIMEOUT", "1")   # else this waits 30s
    neon_writer.close()
    neon_writer.reset_failures()
    try:
        assert neon_writer.upsert_orders([_row("O1")]) == 0
        assert neon_writer.write_failure_count() == 1
    finally:
        neon_writer.close()


def test_an_unset_database_url_is_not_counted_as_a_failure(monkeypatch):
    # Running the scraper without Neon configured is a valid state, not
    # an error to be tallied.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    neon_writer.close()
    neon_writer.reset_failures()
    try:
        assert neon_writer.upsert_orders([_row("O1")]) == 0
        assert neon_writer.write_failure_count() == 0
    finally:
        neon_writer.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_neon_writer.py -v`
Expected: FAIL — `AttributeError: module 'neon_writer' has no attribute 'close'`.

- [ ] **Step 3: Append the pool, the guard and the upsert**

Append to `neon_writer.py`:

```python
import functools
import os
import threading

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

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
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_neon_writer.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the whole suite**

Run: `pytest -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add neon_writer.py tests/test_neon_writer.py
git commit -m "Add the connection pool and upsert_orders

cust_id, status and the two status dates coalesce on conflict rather
than overwrite: scrape_orders never looks at installation status, so
letting its rows win would wipe whatever check_status found an hour
earlier."
```

---

### Task 9: Status and cust-id updates

**Files:**
- Modify: `neon_writer.py` (append)
- Modify: `tests/test_neon_writer.py` (append)

**Interfaces:**
- Consumes: `upsert_orders` and the pool from Task 8.
- Produces: `neon_writer.StatusUpdate` (a `NamedTuple` of `order_number, status, status_latest_date, new_cust_id=""`); `neon_writer.update_order_statuses(updates: list[StatusUpdate]) -> int`; `neon_writer.update_cust_ids(updates: list[tuple[str, str]]) -> int`. Tasks 12 and 13 call these.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_neon_writer.py`:

```python
def test_status_update_sets_status_and_scrape_date(writer, db):
    writer.upsert_orders([_row("O1")])
    n = writer.update_order_statuses(
        [writer.StatusUpdate("O1", "Active", "22 Oct 2025", "")]
    )
    assert n == 1
    row = db.execute(
        "SELECT status, status_latest_date IS NOT NULL, status_scrape_date IS NOT NULL"
        "  FROM unifi_orders WHERE order_number = 'O1'"
    ).fetchone()
    assert row == ("Active", True, True)


def test_status_update_writes_a_timeline_event(writer, db):
    writer.upsert_orders([_row("O1")])
    writer.update_order_statuses([writer.StatusUpdate("O1", "Active", "22 Oct 2025", "")])
    rows = db.execute(
        "SELECT prev_status, status FROM unifi_order_status_events"
        " WHERE order_number = 'O1' ORDER BY id"
    ).fetchall()
    assert rows[-1] == (None, "Active")


def test_repeating_a_status_check_writes_no_new_event(writer, db):
    # The nightly case. status_scrape_date moves every time; the event
    # log must not.
    writer.upsert_orders([_row("O1")])
    writer.update_order_statuses([writer.StatusUpdate("O1", "Active", "22 Oct 2025", "")])
    before = db.execute("SELECT count(*) FROM unifi_order_status_events").fetchone()[0]
    writer.update_order_statuses([writer.StatusUpdate("O1", "Active", "22 Oct 2025", "")])
    writer.update_order_statuses([writer.StatusUpdate("O1", "Active", "22 Oct 2025", "")])
    after = db.execute("SELECT count(*) FROM unifi_order_status_events").fetchone()[0]
    assert after == before


def test_the_cancelled_sentinel_lands_as_null(writer, db):
    writer.upsert_orders([_row("O1")])
    writer.update_order_statuses([writer.StatusUpdate("O1", "-", "", "")])
    got = db.execute("SELECT status FROM unifi_orders WHERE order_number = 'O1'").fetchone()
    assert got[0] is None


def test_status_update_can_carry_a_new_cust_id(writer, db):
    writer.upsert_orders([_row("O1")])
    writer.update_order_statuses([writer.StatusUpdate("O1", "Active", "22 Oct 2025", "20999")])
    got = db.execute("SELECT cust_id FROM unifi_orders WHERE order_number = 'O1'").fetchone()
    assert got[0] == "20999"


def test_blank_cust_id_does_not_wipe_the_existing_one(writer, db):
    writer.upsert_orders([_row("O1")])
    writer.update_order_statuses([writer.StatusUpdate("O1", "Active", "22 Oct 2025", "")])
    got = db.execute("SELECT cust_id FROM unifi_orders WHERE order_number = 'O1'").fetchone()
    assert got[0] == "10555"


def test_update_cust_ids_rewrites_and_logs_an_event(writer, db):
    writer.upsert_orders([_row("O1")])
    before = db.execute("SELECT count(*) FROM unifi_order_status_events").fetchone()[0]
    assert writer.update_cust_ids([("O1", "20999")]) == 1
    got = db.execute("SELECT cust_id FROM unifi_orders WHERE order_number = 'O1'").fetchone()
    assert got[0] == "20999"
    after = db.execute("SELECT count(*) FROM unifi_order_status_events").fetchone()[0]
    assert after == before + 1


def test_update_cust_ids_ignores_unknown_orders(writer, db):
    # check_custid works from sheet rows; an order missing from Neon is
    # not an error, it just has not been backfilled yet.
    assert writer.update_cust_ids([("NOPE", "20999")]) == 1
    assert db.execute("SELECT count(*) FROM unifi_orders").fetchone()[0] == 0


def test_status_updates_of_an_empty_list_is_a_no_op(writer):
    assert writer.update_order_statuses([]) == 0
    assert writer.update_cust_ids([]) == 0
    assert writer.write_failure_count() == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_neon_writer.py -v -k "status_update or cust_id or sentinel"`
Expected: FAIL — `AttributeError: module 'neon_writer' has no attribute 'StatusUpdate'`.

- [ ] **Step 3: Append the update functions**

Append to `neon_writer.py`:

```python
from typing import NamedTuple


class StatusUpdate(NamedTuple):
    """One row's worth of what check_status learned.

    Named rather than a bare 4-tuple because three of the four are
    strings and a positional slip would be silent.
    """
    order_number: str
    status: str
    status_latest_date: str = ""
    new_cust_id: str = ""


_STATUS_SQL = """
UPDATE unifi_orders SET
    status             = %(status)s,
    status_latest_date = coalesce(%(status_latest_date)s, status_latest_date),
    status_scrape_date = %(scraped_at)s,
    cust_id            = coalesce(%(new_cust_id)s, cust_id),
    last_synced        = %(scraped_at)s
WHERE order_number = %(order_number)s
"""


@_guard
def update_order_statuses(updates) -> int:
    """Mirror a StatusBatchWriter flush into Neon."""
    if not updates:
        return 0
    pool = _get_pool()
    if pool is None:
        return 0

    now = datetime.now(LOCAL_TZ)
    params = []
    for item in updates:
        order_number = text(item.order_number)
        if not order_number:
            continue
        params.append(
            {
                "order_number": order_number,
                # "-" becomes NULL: it was never an observed state.
                "status": status_text(item.status),
                "status_latest_date": parse_dt(item.status_latest_date),
                "new_cust_id": text(item.new_cust_id),
                "scraped_at": now,
            }
        )

    if not params:
        return 0

    with pool.connection() as conn:
        _apply_run_id(conn)
        with conn.cursor() as cur:
            cur.executemany(_STATUS_SQL, params)
    return len(params)


_CUST_ID_SQL = """
UPDATE unifi_orders
   SET cust_id = %(new_cust_id)s,
       last_synced = %(scraped_at)s
 WHERE order_number = %(order_number)s
   AND %(new_cust_id)s IS NOT NULL
"""


@_guard
def update_cust_ids(updates) -> int:
    """Mirror check_custid's rewrites. `updates` is [(order_number, new_cust_id)]."""
    if not updates:
        return 0
    pool = _get_pool()
    if pool is None:
        return 0

    now = datetime.now(LOCAL_TZ)
    params = [
        {
            "order_number": text(order_number),
            "new_cust_id": text(new_cust_id),
            "scraped_at": now,
        }
        for order_number, new_cust_id in updates
        if text(order_number)
    ]
    if not params:
        return 0

    with pool.connection() as conn:
        _apply_run_id(conn)
        with conn.cursor() as cur:
            cur.executemany(_CUST_ID_SQL, params)
    return len(params)
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_neon_writer.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add neon_writer.py tests/test_neon_writer.py
git commit -m "Add status and cust-id updates

The '-' sentinel lands as NULL here too, so a cancelled order does not
appear in the churn matrix as a transition into a state nobody ever
observed."
```

---

### Task 10: Scrape-run rows

**Files:**
- Modify: `neon_writer.py` (append)
- Modify: `tests/test_neon_writer.py` (append)

**Interfaces:**
- Consumes: the pool from Task 8, `unifi_scrape_runs` from Task 4.
- Produces: `neon_writer.start_run(month_text, year, scrape_mode, triggered_by, job_id=None) -> int | None`; `neon_writer.finish_run(run_id, status, counts=None, error=None) -> None`. `start_run` also sets the module's current run id so events written afterwards carry it. Task 14 calls these.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_neon_writer.py`:

```python
def test_start_run_returns_an_id_and_marks_it_running(writer, db):
    run_id = writer.start_run("Sep", 2026, "incremental", "cron")
    assert isinstance(run_id, int)
    row = db.execute(
        "SELECT month_text, year, scrape_mode, triggered_by, status"
        "  FROM unifi_scrape_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert row == ("Sep", 2026, "incremental", "cron", "running")


def test_finish_run_records_the_counts(writer, db):
    run_id = writer.start_run("Sep", 2026, "incremental", "cron")
    writer.finish_run(
        run_id, "done",
        counts={"orders_processed": 412, "successful": 409, "skipped": 0, "failed": 3},
    )
    row = db.execute(
        "SELECT status, orders_processed, failed, finished_at IS NOT NULL"
        "  FROM unifi_scrape_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert row == ("done", 412, 3, True)


def test_finish_run_records_an_error(writer, db):
    run_id = writer.start_run("Sep", 2026, "full", "cron")
    writer.finish_run(run_id, "error", error="login timed out")
    row = db.execute(
        "SELECT status, error FROM unifi_scrape_runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert row == ("error", "login timed out")


def test_events_written_during_a_run_carry_its_id(writer, db):
    run_id = writer.start_run("Sep", 2026, "incremental", "cron")
    writer.upsert_orders([_row("O1")])
    got = db.execute(
        "SELECT scrape_run_id FROM unifi_order_status_events WHERE order_number = 'O1'"
    ).fetchone()[0]
    assert got == run_id
    writer.finish_run(run_id, "done")


def test_events_written_outside_a_run_have_no_id(writer, db):
    writer.upsert_orders([_row("O1")])
    got = db.execute(
        "SELECT scrape_run_id FROM unifi_order_status_events WHERE order_number = 'O1'"
    ).fetchone()[0]
    assert got is None


def test_finish_run_clears_the_current_run(writer, db):
    run_id = writer.start_run("Sep", 2026, "incremental", "cron")
    writer.finish_run(run_id, "done")
    writer.upsert_orders([_row("O2")])
    got = db.execute(
        "SELECT scrape_run_id FROM unifi_order_status_events WHERE order_number = 'O2'"
    ).fetchone()[0]
    assert got is None


def test_start_run_without_a_database_returns_none(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    neon_writer.close()
    neon_writer.reset_failures()
    try:
        assert neon_writer.start_run("Sep", 2026, "incremental", "cron") is None
        neon_writer.finish_run(None, "done")     # must not raise
        assert neon_writer.write_failure_count() == 0
    finally:
        neon_writer.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_neon_writer.py -v -k run`
Expected: FAIL — `AttributeError: module 'neon_writer' has no attribute 'start_run'`.

- [ ] **Step 3: Append the run functions**

Append to `neon_writer.py`:

`start_run` does **not** use the shared `@_guard` decorator. That guard returns `0` on failure, and a run id of `0` would be passed to `finish_run` as if it were real. It carries its own try/except returning `None` instead:

```python
def start_run(month_text, year, scrape_mode, triggered_by, job_id=None):
    """Open a scrape-run row and tag subsequent events with it.

    Returns the run id, or None when Neon is not configured or the
    insert failed. The caller passes that value straight back to
    finish_run(), which accepts None.
    """
    global _current_run_id, _failures
    try:
        pool = _get_pool()
        if pool is None:
            return None
        with pool.connection() as conn:
            run_id = conn.execute(
                "INSERT INTO unifi_scrape_runs"
                " (job_id, month_text, year, scrape_mode, triggered_by, status)"
                " VALUES (%s, %s, %s, %s, %s, 'running') RETURNING id",
                (job_id, month_text, year, scrape_mode, triggered_by),
            ).fetchone()[0]
        _current_run_id = run_id
        return run_id
    except Exception as exc:
        _failures += 1
        print(f"⚠️  neon: start_run failed: {exc}")
        return None


@_guard
def finish_run(run_id, status, counts=None, error=None):
    """Close a scrape-run row and stop tagging events with it."""
    global _current_run_id
    _current_run_id = None
    if run_id is None:
        return 0
    pool = _get_pool()
    if pool is None:
        return 0
    counts = counts or {}
    with pool.connection() as conn:
        conn.execute(
            "UPDATE unifi_scrape_runs SET"
            "   status = %s, finished_at = now(), orders_processed = %s,"
            "   successful = %s, skipped = %s, failed = %s, error = %s"
            " WHERE id = %s",
            (
                status,
                counts.get("orders_processed"),
                counts.get("successful"),
                counts.get("skipped"),
                counts.get("failed"),
                error,
            ),
        )
    return 1
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_neon_writer.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add neon_writer.py tests/test_neon_writer.py
git commit -m "Record scrape runs and tag events with the run that saw them

The run id rides on a session setting the trigger reads, so an event
learns which scrape observed it without every INSERT carrying the id."
```

---

### Task 11: The facade and the scrape_orders call site

**Files:**
- Create: `writers.py`
- Create: `tests/test_writers.py`
- Modify: `scrape_orders.py:16-22` (imports), `scrape_orders.py:1121`

**Interfaces:**
- Consumes: `gsheets_writer.upsert_rows`, `neon_writer.upsert_orders`.
- Produces: `writers.upsert_order(ws, row: dict) -> None` and `writers.upsert_orders(ws, rows: list[dict]) -> None`. Sheets is authoritative and may raise; Neon is a shadow write that cannot.

- [ ] **Step 1: Write the failing tests**

`tests/test_writers.py` — these use fakes, no database and no Google:

```python
import pytest

import writers


class FakeSheets:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def upsert_rows(self, ws, rows):
        if self.fail:
            raise RuntimeError("sheets is down")
        self.calls.append((ws, rows))


class FakeNeon:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def upsert_orders(self, rows):
        if self.fail:
            raise RuntimeError("neon is down")
        self.calls.append(rows)
        return len(rows)


@pytest.fixture
def fakes(monkeypatch):
    sheets, neon = FakeSheets(), FakeNeon()
    monkeypatch.setattr(writers, "gsheets_writer", sheets)
    monkeypatch.setattr(writers, "neon_writer", neon)
    return sheets, neon


def test_upsert_order_writes_to_both(fakes):
    sheets, neon = fakes
    writers.upsert_order("WS", {"Order Number": "O1"})
    assert sheets.calls == [("WS", [{"Order Number": "O1"}])]
    assert neon.calls == [[{"Order Number": "O1"}]]


def test_sheets_is_written_first(fakes):
    # If Sheets rejects a row, Neon must not claim it.
    sheets, neon = fakes
    sheets.fail = True
    with pytest.raises(RuntimeError, match="sheets is down"):
        writers.upsert_order("WS", {"Order Number": "O1"})
    assert neon.calls == []


def test_a_neon_failure_does_not_reach_the_caller(monkeypatch):
    # neon_writer guards itself, but the facade must not reintroduce a
    # raise if that guard is ever bypassed.
    sheets, neon = FakeSheets(), FakeNeon(fail=True)
    monkeypatch.setattr(writers, "gsheets_writer", sheets)
    monkeypatch.setattr(writers, "neon_writer", neon)
    writers.upsert_order("WS", {"Order Number": "O1"})     # must not raise
    assert len(sheets.calls) == 1


def test_upsert_orders_passes_the_whole_batch(fakes):
    sheets, neon = fakes
    rows = [{"Order Number": "O1"}, {"Order Number": "O2"}]
    writers.upsert_orders("WS", rows)
    assert sheets.calls == [("WS", rows)]
    assert neon.calls == [rows]
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_writers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'writers'`.

- [ ] **Step 3: Write the facade**

`writers.py`:

```python
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_writers.py -v`
Expected: all PASS.

- [ ] **Step 5: Point scrape_orders at the facade**

In `scrape_orders.py`, the import block at lines 16-22 currently reads:

```python
from gsheets_writer import (
    ensure_tab,
    ensure_tabs_sorted_by_month,
    month_tab_title,
    open_sheet,
    upsert_rows,
)
```

Drop `upsert_rows` from it and add the facade:

```python
import writers
from gsheets_writer import (
    ensure_tab,
    ensure_tabs_sorted_by_month,
    month_tab_title,
    open_sheet,
)
```

At `scrape_orders.py:1121`, replace:

```python
                                upsert_rows(ws, [row_data])
```

with:

```python
                                writers.upsert_order(ws, row_data)
```

- [ ] **Step 6: Verify no other caller of the dropped import remains**

Run: `grep -n "upsert_rows" scrape_orders.py`
Expected: no output. If anything prints, that call site also needs the facade.

Run: `python -c "import scrape_orders"`
Expected: no ImportError.

- [ ] **Step 7: Run the whole suite**

Run: `pytest -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add writers.py tests/test_writers.py scrape_orders.py
git commit -m "Add the writers facade and route scrape_orders through it

Sheets is written first and its exceptions still propagate; Neon is a
shadow write that cannot fail a scrape. The facade exists so retiring
the sheet later is one file, not five call sites."
```

---

### Task 12: The check_status call sites

Three changes in one file. The first is a small refactor: `cancelled_rows` carries only sheet row numbers today, so the `-` writes have no order number to mirror.

**Files:**
- Modify: `check_status.py` — the signature and body of `get_orders_to_check` (lines 80, 134, 167, 169), `StatusBatchWriter` (lines 672-738), the cancelled loop (lines 946-951), and the status write (line 1223)
- Create: `tests/test_status_batch_writer.py`

**Interfaces:**
- Consumes: `neon_writer.StatusUpdate` and `update_order_statuses` from Task 9.
- Produces: `get_orders_to_check` now returns `cancelled_rows` as `list[tuple[int, str]]` of `(row_index, order_number)`. `StatusBatchWriter.add` takes `order_number` as its second positional argument.

- [ ] **Step 1: Write the failing tests**

`tests/test_status_batch_writer.py`:

```python
import pytest

import check_status
from check_status import StatusBatchWriter

HEADERS = [
    "Order Number", "Event Type", "Order Status", "Created Date", "Updated Date",
    "Org Code", "Organization Name", "Name", "Company Name", "Email",
    "Phone Number", "Appointment Date", "Address", "Package", "Device",
    "IC Number", "Creator", "Last Synced", "Cust ID", "Status",
    "Status Latest Date", "Status Scrape Date",
]


class FakeWorksheet:
    def __init__(self, fail=False):
        self.batches = []
        self.fail = fail

    def batch_update(self, batch, value_input_option=None):
        if self.fail:
            raise RuntimeError("sheets is down")
        self.batches.append(batch)


class FakeNeon:
    def __init__(self):
        self.updates = []

    def update_order_statuses(self, updates):
        self.updates.extend(updates)
        return len(updates)


@pytest.fixture
def neon(monkeypatch):
    fake = FakeNeon()
    monkeypatch.setattr(check_status, "neon_writer", fake)
    return fake


def test_flush_mirrors_the_batch_to_neon(neon):
    ws = FakeWorksheet()
    writer = StatusBatchWriter(ws, HEADERS)
    writer.add(5, "ORD1", "Active", "22 Oct 2025")
    writer.flush()

    assert len(ws.batches) == 1
    assert len(neon.updates) == 1
    assert neon.updates[0].order_number == "ORD1"
    assert neon.updates[0].status == "Active"
    assert neon.updates[0].status_latest_date == "22 Oct 2025"


def test_flush_carries_a_new_cust_id(neon):
    ws = FakeWorksheet()
    writer = StatusBatchWriter(ws, HEADERS)
    writer.add(5, "ORD1", "Active", "22 Oct 2025", new_cust_id="20999")
    writer.flush()
    assert neon.updates[0].new_cust_id == "20999"


def test_a_failed_sheet_write_is_not_mirrored(neon):
    # Neon must never claim something the authoritative store rejected.
    ws = FakeWorksheet(fail=True)
    writer = StatusBatchWriter(ws, HEADERS)
    writer.add(5, "ORD1", "Active", "22 Oct 2025")
    writer.flush()
    assert neon.updates == []
    assert writer.write_failures == 1


def test_the_cancelled_sentinel_is_mirrored_with_its_order_number(neon):
    ws = FakeWorksheet()
    writer = StatusBatchWriter(ws, HEADERS)
    writer.add(5, "ORD1", "-")
    writer.flush()
    assert neon.updates[0].order_number == "ORD1"
    assert neon.updates[0].status == "-"


def test_a_row_with_no_order_number_still_writes_to_the_sheet(neon):
    # The sheet is keyed by row index and does not need the order
    # number; only the Neon mirror does.
    ws = FakeWorksheet()
    writer = StatusBatchWriter(ws, HEADERS)
    writer.add(5, "", "Active")
    writer.flush()
    assert len(ws.batches) == 1
    assert neon.updates == []


def test_flush_empties_the_pending_batch(neon):
    ws = FakeWorksheet()
    writer = StatusBatchWriter(ws, HEADERS)
    writer.add(5, "ORD1", "Active")
    writer.flush()
    writer.flush()
    assert len(ws.batches) == 1
    assert len(neon.updates) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_status_batch_writer.py -v`
Expected: FAIL — `AttributeError: module 'check_status' has no attribute 'neon_writer'`, and `add()` taking the wrong arguments.

- [ ] **Step 3: Import neon_writer in check_status**

At `check_status.py:14`, after `from gsheets_writer import month_tab_title, open_sheet`, add:

```python
import neon_writer
```

- [ ] **Step 4: Make cancelled_rows carry order numbers**

At `check_status.py:80`, change the signature's return annotation:

```python
def get_orders_to_check(ws, only_empty: bool = False) -> Tuple[List[Dict], List[Tuple[int, str]]]:
```

At `check_status.py:134`, replace:

```python
                        cancelled_rows.append(row_num)
```

with:

```python
                        # Carries the order number so the "-" write can
                        # be mirrored to Neon, which is keyed by order
                        # number rather than by sheet row.
                        cancelled_rows.append((row_num, order_number))
```

The two `print` statements at lines 167 and the one in `check_all_statuses` use `len(cancelled_rows)` and need no change.

- [ ] **Step 5: Rework StatusBatchWriter**

Replace the `add` method (`check_status.py:702-711`) with:

```python
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
```

Replace the `flush` method (`check_status.py:713-738`) with:

```python
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

        try:
            self.ws.batch_update(batch, value_input_option="USER_ENTERED")
        except Exception as e:
            self.write_failures += len(self.pending)
            err_msg = str(e)
            if err_msg not in self.write_errors:
                self.write_errors.append(err_msg)
            print(f"    Batch write error ({len(self.pending)} rows): {e}")
            self.pending = []
            return

        # Sheets is authoritative and it accepted the batch, so mirror
        # the same rows into Neon. Anything without an order number is
        # skipped: the sheet is keyed by row index, Neon is not.
        neon_writer.update_order_statuses(
            [
                neon_writer.StatusUpdate(order_number, status, status_date, new_cust_id)
                for _row_index, order_number, status, status_date, _timestamp, new_cust_id
                in self.pending
                if order_number
            ]
        )

        self.pending = []
```

Note the early `return` in the exception branch — previously the method fell through to clearing `pending`; now it must clear and return so a failed sheet write is never mirrored.

- [ ] **Step 6: Update the two add() call sites**

At `check_status.py:949-950`, replace:

```python
        for row_idx in cancelled_rows:
            writer.add(row_idx, "-")
```

with:

```python
        for row_idx, order_number in cancelled_rows:
            writer.add(row_idx, order_number, "-")
```

At `check_status.py:1223`, replace:

```python
                writer.add(order["row_index"], status, status_date, new_cust_id=order_updated_cust_id)
```

with:

```python
                writer.add(order["row_index"], order["order_number"], status, status_date, new_cust_id=order_updated_cust_id)
```

- [ ] **Step 7: Verify no other caller of add() remains**

Run: `grep -n "writer.add(" check_status.py check_custid.py`
Expected: exactly the two lines changed above. Any third call site needs the same treatment.

Run: `python -c "import check_status"`
Expected: no ImportError.

- [ ] **Step 8: Run the whole suite**

Run: `pytest -v`
Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
git add check_status.py tests/test_status_batch_writer.py
git commit -m "Mirror status writes into Neon

cancelled_rows now carries the order number alongside the sheet row: the
sheet is keyed by row index and Neon is keyed by order number, so the
'-' writes had nothing to mirror with. A failed sheet write is no longer
mirrored at all -- Neon must not claim what the authoritative store
rejected."
```

---

### Task 13: The check_custid call site

`check_custid.py` looks like a maintenance tool, but `run_daily.py` calls it every night as step 3, and `cust_id` is one of the three columns the status trigger watches. A missed update here is a missing timeline event.

**Files:**
- Modify: `check_custid.py:185` and `check_custid.py:201-215`
- Create: `tests/test_check_custid_mirror.py`

**Interfaces:**
- Consumes: `neon_writer.update_cust_ids` from Task 9.
- Produces: no new public names. `updates` inside `check_custids_for_month` becomes `(row_index, order_number, old_custid, new_custid)`.

- [ ] **Step 1: Write the failing test**

`tests/test_check_custid_mirror.py`. The mirror logic is extracted into a named helper so it can be tested without driving a browser:

```python
import pytest

import check_custid


class FakeWorksheet:
    def __init__(self, fail=False):
        self.batches = []
        self.fail = fail

    def batch_update(self, batch, value_input_option=None):
        if self.fail:
            raise RuntimeError("sheets is down")
        self.batches.append(batch)


class FakeNeon:
    def __init__(self):
        self.updates = []

    def update_cust_ids(self, updates):
        self.updates.extend(updates)
        return len(updates)


@pytest.fixture
def neon(monkeypatch):
    fake = FakeNeon()
    monkeypatch.setattr(check_custid, "neon_writer", fake)
    return fake


def test_write_updates_writes_both_stores(neon):
    ws = FakeWorksheet()
    n = check_custid.write_custid_updates(
        ws, custid_col=19, updates=[(5, "ORD1", "10555", "20999")]
    )
    assert n == 1
    assert len(ws.batches) == 1
    assert neon.updates == [("ORD1", "20999")]


def test_a_failed_sheet_write_is_not_mirrored(neon):
    ws = FakeWorksheet(fail=True)
    with pytest.raises(RuntimeError):
        check_custid.write_custid_updates(
            ws, custid_col=19, updates=[(5, "ORD1", "10555", "20999")]
        )
    assert neon.updates == []


def test_rows_without_an_order_number_are_not_mirrored(neon):
    ws = FakeWorksheet()
    check_custid.write_custid_updates(
        ws, custid_col=19, updates=[(5, "", "10555", "20999")]
    )
    assert len(ws.batches) == 1
    assert neon.updates == []


def test_no_updates_is_a_no_op(neon):
    ws = FakeWorksheet()
    assert check_custid.write_custid_updates(ws, custid_col=19, updates=[]) == 0
    assert ws.batches == []
    assert neon.updates == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_check_custid_mirror.py -v`
Expected: FAIL — `AttributeError: module 'check_custid' has no attribute 'write_custid_updates'`.

- [ ] **Step 3: Import neon_writer**

At `check_custid.py:25`, after `from gsheets_writer import HEADERS, month_tab_title, open_sheet`, add:

```python
import neon_writer
```

- [ ] **Step 4: Add the extracted writer**

Add this function to `check_custid.py`, above `check_custids_for_month`:

```python
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
```

- [ ] **Step 5: Carry the order number into updates**

At `check_custid.py:185`, replace:

```python
                        updates.append((order["row_index"], old_cust_id, best_new))
```

with:

```python
                        updates.append(
                            (order["row_index"], order["order_number"], old_cust_id, best_new)
                        )
```

Update the comment on the `updates = []` line (`check_custid.py:143`) to match:

```python
    updates = []  # (row_index, order_number, old_custid, new_custid)
```

- [ ] **Step 6: Call the extracted writer**

Replace the write block at `check_custid.py:201-215` — everything from `if write and updates:` down to and including the `print(f"  ✅ Updated {len(updates)} rows in the sheet")` line — with:

```python
    if write and updates:
        custid_col = headers.index("Cust ID") + 1
        write_custid_updates(ws, custid_col, updates)
        print(f"  ✅ Updated {len(updates)} rows in the sheet and Neon")
    elif updates and not write:
        print(f"  Run with --write to apply these updates")
```

Keep the `elif` branch exactly as it was.

- [ ] **Step 7: Verify**

Run: `python -c "import check_custid"`
Expected: no ImportError.

Run: `grep -n "updates.append" check_custid.py`
Expected: one line, the four-tuple version.

- [ ] **Step 8: Run the whole suite**

Run: `pytest -v`
Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
git add check_custid.py tests/test_check_custid_mirror.py
git commit -m "Mirror cust-id rewrites into Neon

check_custid looks like a maintenance tool but run_daily calls it every
night, and cust_id is one of the three columns the status trigger
watches. Leaving it out would have meant silently missing timeline
events."
```

---

### Task 14: Record the daily run

**Files:**
- Modify: `run_daily.py`

**Interfaces:**
- Consumes: `neon_writer.start_run` and `finish_run` from Task 10.
- Produces: one `unifi_scrape_runs` row per daily job, `triggered_by="cron"`.

- [ ] **Step 1: Import neon_writer**

In `run_daily.py`, after the existing `from login_manager import login_and_get_context` (line 23), add:

```python
import neon_writer
```

- [ ] **Step 2: Open the run**

In `main()`, immediately after the two existing `print` lines that announce the run (the `f"=== DAILY RUN: ..."` and `f"Months: {months}\n"` pair), add:

```python
    # One run row for the whole nightly job. The month scrapes below are
    # subprocesses, so they cannot own this -- the parent does, and the
    # children write orders under it.
    run_id = neon_writer.start_run(
        month_text=months[0][0] if months else None,
        year=months[0][1] if months else None,
        scrape_mode="incremental",
        triggered_by="cron",
    )
    failed_months = 0
```

- [ ] **Step 3: Count the failing months**

In the Step 2 scrape loop, replace:

```python
        status = "OK" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
```

with:

```python
        if result.returncode != 0:
            failed_months += 1
        status = "OK" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
```

- [ ] **Step 4: Close the run**

Replace the final line of `main()`:

```python
    print(f"\n=== DAILY RUN COMPLETE: {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')} ===")
```

with:

```python
    neon_writer.finish_run(
        run_id,
        "error" if failed_months else "done",
        counts={
            "orders_processed": len(months),
            "successful": len(months) - failed_months,
            "failed": failed_months,
        },
    )
    neon_failures = neon_writer.write_failure_count()
    if neon_failures:
        print(f"\n⚠️  {neon_failures} Neon write(s) failed during this run")
    neon_writer.close()

    print(f"\n=== DAILY RUN COMPLETE: {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')} ===")
```

Note that `orders_processed` here counts *months*, not orders — the parent process never sees individual orders, because each month runs in its own subprocess. That is honest and matches what this row can know; the per-order counts live in the subprocess logs.

- [ ] **Step 5: Verify**

Run: `python -c "import run_daily"`
Expected: this will attempt to run `main()` because the file ends with a bare `asyncio.run(main())`. Instead verify by syntax check only:

Run: `python -m py_compile run_daily.py`
Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add run_daily.py
git commit -m "Record one scrape-run row per nightly job

orders_processed counts months rather than orders: run_daily runs each
month in its own subprocess and never sees individual rows."
```

---

### Task 15: Backfill every month tab

**Files:**
- Create: `backfill_neon.py`
- Create: `tests/test_backfill.py`

**Interfaces:**
- Consumes: `neon_writer.upsert_orders`; `gsheets_writer.get_all_month_tabs` and `open_sheet`.
- Produces: `backfill_neon.rows_from_values(values: list[list[str]]) -> list[dict]`; `backfill_neon.backfill_tab(ws, chunk_size=500) -> int`; a `__main__` entry point.

- [ ] **Step 1: Write the failing tests**

`tests/test_backfill.py`:

```python
import pytest

import backfill_neon

HEADERS = ["Order Number", "Order Status", "Created Date", "Cust ID", "Status"]


class FakeWorksheet:
    def __init__(self, values, title="Sep 2026"):
        self._values = values
        self.title = title

    def get_all_values(self):
        return self._values


def test_rows_from_values_maps_headers_to_dicts():
    rows = backfill_neon.rows_from_values(
        [HEADERS, ["'O1", "Completed", "22 Oct 2025 09:30", "10555", "Active"]]
    )
    assert rows == [
        {
            "Order Number": "'O1",
            "Order Status": "Completed",
            "Created Date": "22 Oct 2025 09:30",
            "Cust ID": "10555",
            "Status": "Active",
        }
    ]


def test_rows_from_values_skips_blank_order_numbers():
    rows = backfill_neon.rows_from_values(
        [HEADERS, ["", "Completed", "", "", ""], ["O2", "", "", "", ""]]
    )
    assert [r["Order Number"] for r in rows] == ["O2"]


def test_rows_from_values_pads_short_rows():
    # Google Sheets truncates trailing empty cells.
    rows = backfill_neon.rows_from_values([HEADERS, ["O1", "Completed"]])
    assert rows[0]["Status"] == ""
    assert rows[0]["Cust ID"] == ""


def test_rows_from_values_handles_an_empty_tab():
    assert backfill_neon.rows_from_values([]) == []
    assert backfill_neon.rows_from_values([HEADERS]) == []


def test_backfill_tab_chunks_its_writes(monkeypatch):
    calls = []
    monkeypatch.setattr(
        backfill_neon.neon_writer, "upsert_orders", lambda rows: calls.append(len(rows))
    )
    values = [HEADERS] + [[f"O{i}", "Completed", "", "", ""] for i in range(1200)]
    n = backfill_neon.backfill_tab(FakeWorksheet(values), chunk_size=500)
    assert n == 1200
    assert calls == [500, 500, 200]
```

And an end-to-end idempotency test against the real schema — this is the property that makes the script re-runnable after a partial failure. Append to `tests/test_neon_writer.py`:

```python
def test_backfilling_twice_writes_no_second_event(writer, db):
    # First pass: one INSERT event per order, prev_status NULL. That is
    # what the portal renders as "State at migration".
    rows = [_row(f"O{i}", **{"Status": "Active"}) for i in range(20)]
    writer.upsert_orders(rows)
    after_first = db.execute("SELECT count(*) FROM unifi_order_status_events").fetchone()[0]
    assert after_first == 20

    # Second pass is an UPDATE the trigger guard suppresses.
    writer.upsert_orders(rows)
    after_second = db.execute("SELECT count(*) FROM unifi_order_status_events").fetchone()[0]
    assert after_second == 20

    prev_statuses = db.execute(
        "SELECT DISTINCT prev_status FROM unifi_order_status_events"
    ).fetchall()
    assert prev_statuses == [(None,)]
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_backfill.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backfill_neon'`.

- [ ] **Step 3: Write the backfill script**

`backfill_neon.py`:

```python
"""One-time: load every month tab of the Google Sheet into Neon.

Run once at cutover, before the dual-write period begins, so the portal
has real history on day one.

THE TRIGGER STAYS ENABLED. Each row produces exactly one INSERT event
with prev_status NULL, which is precisely the "State at migration" row
the timeline needs. Disabling it would leave every order with no
starting state.

Re-running is safe: the second pass is an UPDATE whose guard finds
nothing meaningfully changed, so no new events appear. That is what
makes this recoverable after a partial failure.

Usage:
  python backfill_neon.py              # every month tab
  python backfill_neon.py Sep 2026     # one tab
"""

import sys

from dotenv import load_dotenv

load_dotenv()

import neon_writer
from gsheets_writer import get_all_month_tabs, month_tab_title, open_sheet

CHUNK_SIZE = 500


def rows_from_values(values):
    """Turn a worksheet's raw cell grid into Sheet-shaped row dicts.

    Google Sheets truncates trailing empty cells, so short rows are
    padded rather than skipped.
    """
    if not values or len(values) < 2:
        return []

    headers = values[0]
    rows = []
    for raw in values[1:]:
        if not raw:
            continue
        padded = list(raw) + [""] * (len(headers) - len(raw))
        row = dict(zip(headers, padded))
        if not str(row.get("Order Number", "")).strip().lstrip("'"):
            continue
        rows.append(row)
    return rows


def backfill_tab(ws, chunk_size=CHUNK_SIZE) -> int:
    """Upsert one month tab into Neon. Returns the number of rows sent."""
    rows = rows_from_values(ws.get_all_values())
    if not rows:
        print(f"  {ws.title}: empty, skipped")
        return 0

    sent = 0
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start:start + chunk_size]
        neon_writer.upsert_orders(chunk)
        sent += len(chunk)
        print(f"  {ws.title}: {sent}/{len(rows)}")
    return sent


def main(argv):
    spread = open_sheet()

    if len(argv) >= 2:
        titles = [month_tab_title(argv[0], int(argv[1]))]
    else:
        titles = get_all_month_tabs(spread)

    print(f"Backfilling {len(titles)} tab(s) into Neon")
    total = 0
    for title in titles:
        try:
            ws = spread.worksheet(title)
        except Exception as exc:
            print(f"  {title}: could not open ({exc})")
            continue
        total += backfill_tab(ws)

    failures = neon_writer.write_failure_count()
    print(f"\nDone: {total} rows sent, {failures} Neon write failure(s)")
    if failures:
        print("Re-run this script — it is idempotent and writes no duplicate events.")
    neon_writer.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_backfill.py tests/test_neon_writer.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the whole suite**

Run: `pytest -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add backfill_neon.py tests/test_backfill.py tests/test_neon_writer.py
git commit -m "Add the one-time sheet-to-Neon backfill

The trigger stays enabled: each backfilled row produces exactly one
INSERT event with prev_status NULL, which is what the timeline renders
as 'State at migration'. A second pass writes no new events, so a
partial failure is recoverable by re-running."
```

---

## After the plan

The code is done; the rollout is not. In order:

1. **Apply the schema to the Neon dev branch** and run the suite against it once with `TEST_DATABASE_URL` pointed there, to confirm nothing depends on a local-Postgres quirk.
2. **Run `backfill_neon.py` against the dev branch.** Check the row count against the sheet and spot-check a month's `unifi_monthly_stats` against the Telegram message for the same month — that is acceptance criterion 4, and it is the one that catches a coercion bug.
3. **Apply to `production` and register the portal's migration in the same sitting.** Between the apply and `prisma migrate resolve --applied`, the portal's migration state is drifted and `prisma migrate dev` may offer a reset. Minutes, not days.
4. **Run the dual-write for a week**, then compare Sheets against Neon month by month before anything reads from Neon.

Out of scope here and tracked in the parent spec: the Flask API lockdown (§8.3), the portal's pages and Prisma models, the channels import (§6.4), retiring the sheet, and the n8n repoint.
