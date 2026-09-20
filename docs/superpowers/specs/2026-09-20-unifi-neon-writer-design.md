# Design — Neon schema and dual-write in the scraper

Date: 2026-09-20
Repo: `jobhuntersaipro-design/unifi-scraper`
Implements: steps 1–2 of `docs/bizzflow-admin-unifi-spec.md` §12, from the scraper side.

This covers the canonical Postgres schema and the writer that fills it. The
portal pages (§5–§7), the channels CRUD and import (§6), and the Flask API
lockdown (§8.3) are out of scope and land later.

---

## 1. Why the schema lives here

The parent spec puts the schema in the portal repo under Prisma. We inverted
that: `sql/001_unifi_schema.sql` in this repo is canonical, and the portal's
hand-written Prisma migration is a copy of it, registered with
`prisma migrate resolve`.

The reason is ordering. The scraper is what fills these tables, and it can start
doing so before a single portal page exists — which is the whole point of a week
of dual-write before anything switches over. If Prisma owned the DDL, nothing in
this repo could run until the portal work landed.

The drift risk is real and is handled by one mechanism: the SQL file carries a
`-- schema-checksum: <sha256 of every line below the header>` comment. A test in
this repo asserts the header matches the body, so the checksum cannot go stale
unnoticed; the portal's migration carries the same value, and a mismatch there is
a failed check rather than a divergence discovered by a 500 in production.

---

## 2. Decisions

| Question | Decision |
|---|---|
| Where the DDL lives | `sql/001_unifi_schema.sql` here, canonical. Prisma mirrors it. |
| Dual-write shape | A `writers.py` facade. Sheets stays authoritative; Neon is a shadow write that can never fail a scrape. |
| DB credentials | A dedicated `unifi_scraper` Postgres role with grants on `unifi_*` only, in the portal's Neon project and branch. |
| History | One-time `backfill_neon.py` over every month tab. |
| Volume assumption | >2,000 orders/month, so ~50k+ rows at backfill. Chunked commits, and §5's server-side pagination is load-bearing. |

### 2.1 Three judgement calls worth re-reading

**The `-` status sentinel becomes NULL.** In the sheet, `Status = "-"` means
"this order is cancelled, don't bother querying it". Stored literally, §7's churn
matrix fills with transitions into `-`, which nobody ever observed.
`order_status = 'Cancelled'` already carries that meaning. `_coerce_row` maps
`"-"` to NULL for `status` only.

**The three `backfill_*.py` scripts do not dual-write; `check_custid.py` does.**
This distinction matters and the first draft of this design got it wrong.
`check_custid.py` looks like a maintenance tool but `run_daily.py` calls it every
night as step 3, and the column it writes — `cust_id` — is one of the three the
status trigger watches. A missed update there is a missing timeline event, not
just a stale cell. The `backfill_*.py` scripts are genuinely one-off; whatever
they touch, the next full scrape reconciles through the normal path.

**Neon write failures are counted and printed, not stored in a new column.**
`unifi_scrape_runs` as specified in §4.2 has no field for this, and extending the
schema before the portal reads it invites a second migration. The count goes to
stdout and into the run summary.

---

## 3. Approach, and what was rejected

`neon_writer.py` accepts the same Sheet-shaped row dicts that
`gsheets_writer.upsert_rows` accepts — keys like `"Order Number"`,
`"Created Date"` — and does its own coercion to typed columns.

Two alternatives were considered:

- **An `OrderRecord` dataclass both writers render from.** Cleaner in the
  abstract, but `check_status.py` writes *partial* updates: a status, a date and
  sometimes a new cust id, for one row. There is no whole record in hand, so this
  forces a read-modify-write on every status check.
- **Neon-shaped records with `gsheets_writer` adapting.** The right end state once
  the sheet is retired, and the wrong thing to do while the sheet is still what
  people depend on.

The chosen shape leaves the working scrape path's data shapes untouched, which is
what makes a comparison between Sheets and Neon meaningful.

---

## 4. Files

```
sql/001_unifi_schema.sql   canonical DDL: tables, generated column, trigger, views, grants
neon_writer.py             pool, upsert_orders, update_order_statuses, scrape-run rows
writers.py                 facade over gsheets_writer + neon_writer
backfill_neon.py           one-time, all month tabs -> Neon
tests/conftest.py          fixture: fresh DB built from sql/001 alone
tests/test_coercion.py     row-dict -> typed column mapping, no DB needed
tests/test_schema.py       trigger, generated column, views, checksum header
tests/test_neon_writer.py  upsert and status-update semantics against a real Postgres
.env.example               new keys, documented
```

`requirements.txt` gains `psycopg[binary,pool]==3.2.*` and `pytest`. Nothing else.

---

## 5. The schema file

The four models of §4.2, unchanged in column names and types. Beyond them, three
things Prisma cannot express.

### 5.1 Generated column

```sql
display_name text GENERATED ALWAYS AS (
  coalesce(nullif(fleet_label,''), nullif(plate,''), channel_name)
) STORED
```

The fallback chain is enforced by the database rather than by a TypeScript
expression that can drift from a Python one.

### 5.2 The status trigger

`AFTER INSERT OR UPDATE ON unifi_orders FOR EACH ROW`, with a guard that is the
single most important line in this design:

```sql
IF TG_OP = 'UPDATE' AND NOT (
     NEW.order_status IS DISTINCT FROM OLD.order_status
  OR NEW.status       IS DISTINCT FROM OLD.status
  OR NEW.cust_id      IS DISTINCT FROM OLD.cust_id
) THEN
  RETURN NULL;
END IF;
```

`status_scrape_date` and `last_synced` change on every single check and are
deliberately absent from that list. If they were included, one nightly status run
with zero real changes would write one event per order, and acceptance criterion 2
would fail on the first night.

On INSERT the trigger writes one event with `prev_order_status` and `prev_status`
NULL. §7 renders those as "State at migration".

### 5.3 Views

`unifi_order_status_timeline`, `unifi_monthly_stats`,
`unifi_monthly_channel_breakdown`, `unifi_unmapped_channels`.

`unifi_monthly_stats` uses §5's definitions verbatim, because acceptance criterion
4 is that the portal's tiles match the Telegram message to the row:

- `completed` — `lower(order_status) = 'completed'`, exact
- `cancelled` — `order_status` matching `cancel|void|failed`, substring
- everything else contributes to the total only

### 5.4 Grants

A `unifi_scraper` role with:

- `SELECT, INSERT, UPDATE` on `unifi_orders`
- `SELECT, INSERT` on `unifi_order_status_events`, plus `USAGE` on its sequence
- `SELECT, INSERT, UPDATE` on `unifi_scrape_runs`, plus `USAGE` on its sequence
- `SELECT` only on `unifi_channels` — the portal owns that table (§2) and the
  scraper never writes it

The events grant is not optional. A trigger function runs with the privileges of
the role that caused it to fire, not the table owner, so without INSERT on the
events table every scraper write fails. The alternative is `SECURITY DEFINER`,
which is more privilege than a row-level audit trigger should carry.

---

## 6. `neon_writer.py`

A module-level `psycopg_pool.ConnectionPool`, opened lazily on first use, with
`min_size=1`. Small, because of how the daily job is structured: `run_daily.py`
runs each month's scrape as a *subprocess* (`run_scrape_subprocess`), so six
short-lived pools are created in sequence rather than one long-lived pool serving
six months. A large `min_size` would open connections that are discarded seconds
later. Neon also closes idle connections, which is the other reason this is a pool
rather than a single long-lived connection.

Public surface:

```python
upsert_orders(rows: list[dict]) -> int
update_order_statuses(updates: list[StatusUpdate]) -> int
update_cust_ids(updates: list[tuple[str, str]]) -> int   # (order_number, new_cust_id)
start_run(month_text, year, scrape_mode, triggered_by) -> int | None
finish_run(run_id, status, counts, error=None) -> None
write_failure_count() -> int
```

`StatusUpdate` is a `NamedTuple` of `(order_number, status, status_latest_date,
new_cust_id)` rather than a bare 4-tuple, because three of the four are strings
and a positional mistake would be silent.

Batching is `cur.executemany` — psycopg 3 pipelines it, so a list of rows costs
roughly one round trip rather than one per row.

Upserts are `INSERT ... ON CONFLICT (order_number) DO UPDATE`, with the update
list naming columns explicitly. It never touches `display_name` (generated) and
never clobbers a populated `status` with a NULL from a scrape that did not check
status.

`_coerce_row` is the only place that knows about the sheet's quirks:

- strips the leading `'` that forces Google Sheets to treat order numbers as text.
  The facade is called *after* `scrape_orders.py` has already added that prefix
  for the sheets branch, so the coercion must handle both forms.
- parses the date formats `scrape_orders.py` emits into `timestamptz`
- maps `""` to NULL
- maps `"-"` to NULL for `status` (see §2.1)
- stores the whole incoming dict in `raw` as JSONB

Every public function catches, logs, increments the failure counter and returns.
Nothing in this module can raise into a scrape.

---

## 7. Call-site changes

Five, of which two are small refactors to carry an order number that is already
in scope but not passed along.

**[scrape_orders.py:1121](../../../scrape_orders.py#L1121)** — `upsert_rows(ws, [row_data])`
becomes `writers.upsert_order(ws, row_data)`.

**[check_status.py:1223](../../../check_status.py#L1223)** — pass
`order["order_number"]` into `writer.add`. It is already in the dict that
`get_orders_to_check` builds.

**[check_status.py:950](../../../check_status.py#L950)** — refactor.
`get_orders_to_check` returns `cancelled_rows` as a list of sheet row numbers with
no order number attached, so the `-` writes cannot be mirrored. It becomes
`list[tuple[int, str]]` of `(row_index, order_number)`.

**`StatusBatchWriter.flush`** ([check_status.py:713](../../../check_status.py#L713)) —
after the Sheets `batch_update` succeeds, mirror the same batch through
`neon_writer.update_order_statuses`. Order matters: if the sheet write fails, the
Neon write is skipped, so Neon never claims something the authoritative store
rejected.

**[check_custid.py:185](../../../check_custid.py#L185)** — refactor. `updates` is
built as `(row_index, old_custid, new_custid)`; it gains `order["order_number"]`,
which is already present in the `order` dict from `get_old_custid_orders`. After
the `batch_update` at [check_custid.py:213](../../../check_custid.py#L213), mirror
through `neon_writer.update_cust_ids`.

### 7.1 Scrape-run rows

`run_daily.py` wraps its run with `start_run(..., triggered_by="cron")` and
`finish_run(...)`, recording one `unifi_scrape_runs` row per daily job. The month
subprocesses do not write run rows — the parent owns the run, the children write
orders. `api_server.py` will write run rows with `triggered_by="admin"` when §8
lands; that is out of scope here.

### 7.2 The facade

`writers.py` is thin on purpose:

```python
def upsert_order(ws, row):
    gsheets_writer.upsert_rows(ws, [row])   # authoritative; may raise
    neon_writer.upsert_orders([row])        # shadow; never raises
```

It exists so that retiring Sheets later is an edit to one file rather than a hunt
through five call sites.

---

## 8. Backfill

`backfill_neon.py` walks `get_all_month_tabs`, reads each tab with
`get_all_values`, and upserts in chunks of 500 rows per transaction with progress
output per tab. At >2,000 orders/month across the sheet's history this is on the
order of 50k rows — minutes.

**The trigger stays enabled during backfill.** Each row produces exactly one
INSERT event with `prev_status IS NULL`, which is precisely the "State at
migration" row §7 wants to render. Disabling the trigger would leave the timeline
with no starting state.

Re-running the backfill is safe and produces no new events: the second pass is an
UPDATE whose guard finds nothing meaningfully changed. That property is worth a
test, because it is what makes the script re-runnable after a partial failure.

---

## 9. Testing

Test-driven, per the repo's normal workflow. This repo has no tests today, so
`tests/` and a pytest dependency arrive with this change.

The trigger, the generated column and the views need a real server — acceptance
criterion 1 is that they exist on a fresh database built from the SQL alone. A
local Docker Postgres provides that: fast, free, no network, and
Postgres-compatible with Neon for every feature used here. If Docker is not
available on the machine doing the work, the fallback is a scratch Neon branch
pointed at by `TEST_DATABASE_URL`; the fixture takes whichever it finds.

`conftest.py` creates a throwaway database, applies `sql/001_unifi_schema.sql`,
and drops it afterwards. The coercion tests need no database at all.

What the tests cover, mapped to the parent spec's criteria:

| Criterion | Test |
|---|---|
| 1 — fresh DB from migrations alone | fixture applies the SQL file; assert trigger and all four views exist |
| 2 — insert then status update yields exactly two events; a no-op update yields none | direct SQL, no writer involved |
| 3 — `display_name` fallback chain, including blank rows | parametrised over the sheet's known blanks (`RV10558`, `AF10111`, `RAGT10896`) |
| 4 — stat tiles match Telegram | `unifi_monthly_stats` against a fixture month with known completed/cancelled/other counts |
| 6 — unmapped org codes surface | insert an order whose org code is absent from channels; assert it appears in the view |

Beyond the parent spec's criteria:

- the checksum header matches the body of the SQL file
- backfill idempotency: a second pass writes no new events
- a Neon outage leaves the Sheets path working and the scrape succeeding
- `status_scrape_date` moving on its own produces no event (the guard, directly)

---

## 10. Environment

New key, added to a new `.env.example` alongside the two that already exist:

```
# Neon — the scraper's own role, not the portal's connection string
DATABASE_URL=postgresql://unifi_scraper:...@...neon.tech/...?sslmode=require
```

Note that `api_server.py` does not call `load_dotenv()` today, unlike the
`run_*.py` entry points. It will need to once it reads `DATABASE_URL`, or the
droplet's service definition must supply the environment. Not part of this change,
but it is the trap waiting in the next one.

---

## 11. Corrections to the parent spec

Recorded here and folded back into `docs/bizzflow-admin-unifi-spec.md`:

1. **§8.1 names endpoints that do not exist.** It says `POST /scrape` then poll
   `/status/<job_id>`. In `api_server.py`, `/scrape` is *blocking* and returns only
   when the scrape completes; the async job API is `POST /jobs` →
   `GET /jobs/<job_id>`. `/status` is a summary endpoint taking no job id.
   "Sync now" with a 10-second abort must call `/jobs`.
2. **§8.3's CORS item is a no-op.** There is no CORS handling in the repo and
   `flask-cors` is not a dependency. A Vercel server action is server-to-server and
   sends no browser origin. The exposure is the missing authentication, which §8.3
   item 1 does cover.
3. **`scrape_locks` is defined twice** (`api_server.py` lines 24 and 297). The
   second definition discards anything held by the first.
4. **The lock is global, not per-month.** `POST /jobs` rejects a new job if *any*
   job is running, so §8.1's "a sync for Sep 2026 is already running" toast will
   fire for a sync of any month.
5. **Open question 1 is answered:** >2,000 orders/month, so the §5 pagination and
   index plan stands as written.

Items 1–4 belong to the §8 work and are not fixed here.

---

## 12. Out of scope

The Flask API lockdown (§8.3) · anything in the portal repo, including the Prisma
models and migration · the channels import from the sheet (§6.4, portal owns it) ·
retiring the Google Sheet · the n8n repoint.
