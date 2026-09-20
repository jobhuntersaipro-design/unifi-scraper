# Design — one Sheet tab into a Neon table (`sheet_to_neon.py`)

Date: 2026-09-20
Repo: `jobhuntersaipro-design/unifi-scraper`
Status: spec only. No code written yet.

Read one worksheet of the **Unifi Orders** spreadsheet — the tab addressed by
`gid=1987728467` — and upsert it into a new, standalone Neon table
`unifi_sheet_orders`. On demand or under cron, idempotent, and independent of
the scraper's `unifi_orders` pipeline.

Source:
`https://docs.google.com/spreadsheets/d/11WAvdweVtPLML-TtMmdNwY6EnqBywYLmYpkm1mjfb9M/edit?gid=1987728467`

---

## 1. What the source actually is

Worth stating plainly, because it constrains everything below:

- The spreadsheet is **"Unifi Orders"** — the same document `gsheets_writer.py`
  writes, owned by `jobhunters.ai.pro@gmail.com`, ~4.9 MB.
- It holds ~20 month tabs, Feb 2025 → Sep 2026, and **every one carries the
  identical 22-column header**:

  `Order Number · Event Type · Order Status · Created Date · Updated Date ·
  Org Code · Organization Name · Name · Company Name · Email · Phone Number ·
  Appointment Date · Address · Package · Device · IC Number · Creator ·
  Last Synced · Cust ID · Status · Status Latest Date · Status Scrape Date`

- `gid=1987728467` is therefore one of those month tabs. **Which** month it is
  cannot be resolved without the Sheets API, and this design never needs to
  know: the script looks the worksheet up *by gid* and reads the tab's own
  title off the result.

That last point is the reason the gid is the input and not a month name. The
link the work came in on is the address; nothing has to agree about "Sep 2026"
vs `"Sep"` vs `"September 2026"` (`gsheets_writer.month_tab_title` maintains
three spellings of that mapping today).

## 2. Decisions

| Question | Decision |
|---|---|
| Target | A new table `unifi_sheet_orders`. Not `unifi_orders`. |
| DDL | A second file, `sql/002_unifi_sheet_orders.sql`. `001` is not touched, so its checksum and the portal's mirrored Prisma migration stay valid. |
| Tab selection | By `gid`, defaulting to `1987728467`. `--gid` overrides. |
| Cadence | Re-runnable any time. Upsert on `order_number`; a second run changes nothing but `imported_at`. |
| Failure policy | **Loud.** Exceptions propagate, exit code is non-zero, an unset `DATABASE_URL` is a hard error. |
| Connection | `psycopg.connect()` directly, as `apply_schema.py` does. No pool. |
| Coercion | Reuses `neon_writer.coerce_row()` and `backfill_neon.rows_from_values()` unchanged. |
| Trigger | None on the new table. |

### 2.1 Four judgement calls worth re-reading

**A separate table, not `unifi_orders`.** `unifi_orders` is not an inert store:
`unifi_orders_status_change` fires on every insert and update, and three
columns (`order_status`, `status`, `cust_id`) are compared to decide whether an
event is written. An importer pointed at it would inject status-timeline events
for rows nobody observed changing, and would race the nightly scrape for the
same primary keys. `unifi_sheet_orders` is a literal mirror of a tab: what the
sheet says, when we read it, and nothing inferred.

**This script is loud where `neon_writer` is deliberately silent.**
`neon_writer`'s entire contract is that nothing may raise into a scrape — every
public function is `@_guard`-wrapped, and an unset `DATABASE_URL` is a valid
"Neon not configured, carry on" state. For a standalone importer that contract
is exactly backwards: a swallowed write failure is a silent data gap that a
cron entry reports as success. So `sheet_to_neon.py` borrows `neon_writer`'s
*pure* helpers (`coerce_row`, `text`, `parse_dt`) and **none** of its guarded
write path. It follows `backfill_neon.py`'s precedent of refusing to run
without `DATABASE_URL` rather than quietly doing nothing.

**`imported_at` moves on every run, and that is safe here precisely because
there is no trigger.** In `unifi_orders`, `last_synced` and
`status_scrape_date` are kept *out* of the trigger's comparison because they
move on every check and would otherwise write one event per order per quiet
night. `unifi_sheet_orders` has no trigger and no event table, so a
freshness stamp that always moves costs nothing and buys the one query that
matters operationally: "what did the last run not see?"

**Coercion is shared, not copied.** `neon_writer._FIELD_MAP` is the only place
in the repo that knows the sheet's string formats — the `%d %b %Y %H:%M`
family, the leading-apostrophe strip, the `" - "` appointment range, and the
`"-"` status sentinel. Two independent readers of the same tab that disagree
about what `"-"` means would be worse than the coupling. The two tables land
identically-typed values, which also makes them directly diffable
(§7.2).

## 3. Approach, and what was rejected

**Rejected: extend `backfill_neon.py` with a `--gid` flag.** It is close — it
already walks month tabs into Neon — but everything it does goes through
`neon_writer`'s guarded path into `unifi_orders`, trigger and all. The two
scripts want opposite failure policies and different tables; bolting a flag on
would mean one file with two contracts.

**Rejected: a CSV export of the tab.** `docs.google.com/.../export?gid=…`
needs the same credentials and gives up gspread's typed access and the header
row handling that already exists.

**Rejected: `DELETE` then bulk insert.** Simple, but a failed run leaves the
table empty rather than stale — the worse of the two states for something a
dashboard may read.

**Rejected: a surrogate `bigserial` key.** `order_number` is already unique
within a tab and is how every other table in this schema is joined.

## 4. Files

| File | Change |
|---|---|
| `sql/002_unifi_sheet_orders.sql` | New. Table, indexes, grants, guarded role block. |
| `sheet_to_neon.py` | New. The importer. ~150 lines. |
| `apply_schema.py` | Applies `002` after `001`; `--verify` learns the new table. |
| `tests/conftest.py` | The `db` fixture applies both SQL files in order. |
| `tests/test_sheet_to_neon.py` | New. |
| `.env.example` | Two optional overrides documented. |
| `sql/001_unifi_schema.sql` | **Untouched.** Its checksum must not change. |

## 5. The table

```sql
-- sql/002_unifi_sheet_orders.sql
--
-- A literal mirror of one Google Sheet tab. Deliberately NOT unifi_orders:
-- that table carries the status trigger, and an import must not manufacture
-- timeline events for transitions nobody observed.

CREATE TABLE IF NOT EXISTS unifi_sheet_orders (
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
    status             text,
    status_latest_date timestamptz,
    status_scrape_date timestamptz,
    last_synced        timestamptz,
    -- provenance: which tab this row came out of, and when we read it
    source_gid         bigint,
    source_tab         text,
    source_row         integer,
    imported_at        timestamptz NOT NULL DEFAULT now(),
    raw                jsonb
);

CREATE INDEX IF NOT EXISTS unifi_sheet_orders_source_gid_idx
    ON unifi_sheet_orders (source_gid);
CREATE INDEX IF NOT EXISTS unifi_sheet_orders_created_date_idx
    ON unifi_sheet_orders (created_date DESC);
CREATE INDEX IF NOT EXISTS unifi_sheet_orders_imported_at_idx
    ON unifi_sheet_orders (imported_at DESC);
```

Columns 1–22 are the same names and types `unifi_orders` uses, so the two are
comparable with a plain join. The four added ones answer, for any row, *which
tab, which row of it, and when*.

`source_row` is the 1-based spreadsheet row number, header included — the
number in the row gutter, so a bad row can be opened in the browser directly.

**Grants.** Roles are cluster-wide and `001` already creates `unifi_scraper`
inside a guarded `DO` block; `002` repeats that guard so it can be applied to a
database that has never seen `001`, then grants on its own table only:

```sql
DO $role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'unifi_scraper') THEN
        CREATE ROLE unifi_scraper LOGIN;
    END IF;
END
$role$;

GRANT USAGE ON SCHEMA public TO unifi_scraper;
GRANT SELECT, INSERT, UPDATE, DELETE ON unifi_sheet_orders TO unifi_scraper;
```

`DELETE` is granted for `--prune` (§6.4). There is no sequence to grant on —
the primary key comes from the sheet.

`002` carries no `schema-checksum` header. That mechanism exists to stop
`001` from drifting against the portal's copy of it; the portal does not know
about this table.

## 6. `sheet_to_neon.py`

### 6.1 Configuration

Defaults are baked in from the URL, so the script runs with no new
configuration at all:

```python
SPREADSHEET_ID = os.environ.get(
    "SHEET_IMPORT_SPREADSHEET_ID",
    "11WAvdweVtPLML-TtMmdNwY6EnqBywYLmYpkm1mjfb9M",
)
DEFAULT_GID = int(os.environ.get("SHEET_IMPORT_GID", "1987728467"))
```

Note this is *not* `GOOGLE_SHEETS_SPREADSHEET_ID`. They are very probably the
same document, but the scraper's variable means "the sheet I write scrapes
into", and silently inheriting it would point this importer somewhere else the
day that changes. Credentials are the existing `service_account.json`, same
file `gsheets_writer.open_sheet()` uses.

### 6.2 Resolving the tab

```python
spread = gspread.service_account(filename="service_account.json").open_by_key(SPREADSHEET_ID)
ws = spread.get_worksheet_by_id(int(gid))    # gspread 5.12 compares against ws.id (int)
```

`get_worksheet_by_id` raises `WorksheetNotFound`; the script catches it only to
print the gid and the titles that *do* exist, then exits 1. The `int()` cast is
load-bearing — gspread's signature accepts `Union[str, int]` but matches on the
integer `ws.id`, so a string gid silently finds nothing.

### 6.3 Reading and coercing

```python
rows = backfill_neon.rows_from_values(ws.get_all_values())   # pure function
```

This keeps one definition of "how a Sheet grid becomes row dicts" — including
the two cases that already cost someone an afternoon: Sheets truncates trailing
empty cells (short rows are padded, not skipped), and a row with *more* cells
than the header is logged rather than silently truncated by `zip()`. Blank
order numbers are dropped.

Importing a pure helper out of a one-off script is mild coupling and is the
right trade against a second, drifting copy. `backfill_neon` does nothing at
import time beyond `load_dotenv()`.

Then per row:

```python
values = neon_writer.coerce_row(row)          # 22 typed columns + last_synced + raw
values["raw"] = Jsonb(values["raw"])
values.update(source_gid=gid, source_tab=ws.title, source_row=n, imported_at=started_at)
```

One deviation from `coerce_row`'s behaviour is worth spelling out: it falls
back to `datetime.now()` when the sheet's `Last Synced` cell is blank or
unparseable. For a mirror table that is a small lie — the sheet said nothing.
`sheet_to_neon.py` re-derives the column itself (`parse_dt(row.get("Last
Synced"))`, no fallback) so `last_synced IS NULL` means "the cell was empty",
and `imported_at` carries the run's own clock.

`started_at` is captured once, before the read, and stamped on every row of the
run — so one run yields exactly one `imported_at` value and §7.2's staleness
query has a clean boundary.

### 6.4 Writing

One `psycopg.connect(DATABASE_URL)`, one transaction per chunk of 500,
`cur.executemany` over:

```sql
INSERT INTO unifi_sheet_orders (<27 columns>) VALUES (<%(named)s …>)
ON CONFLICT (order_number) DO UPDATE SET <all 26 non-key columns> = EXCLUDED.…
```

Every non-key column is overwritten, `imported_at` included. There is no
`coalesce` half of the update as there is in `neon_writer._UPSERT_SQL` — that
exists because a scrape must not blank what a status check found, and here
there is exactly one writer: the tab. If a cell was cleared in the sheet, the
table should say so.

A row whose `order_number` already exists under a *different* `source_gid` is
overwritten and its provenance columns rewritten to the new tab. With one gid
that cannot happen; with `--gid` it can, and last writer wins. Documented in
the module docstring, not defended against.

`--prune` (optional, off by default) deletes rows for this gid that this run
did not see:

```sql
DELETE FROM unifi_sheet_orders
 WHERE source_gid = %(gid)s AND imported_at < %(started_at)s
```

It runs in the same transaction as the final chunk, so a partial import can
never prune rows it merely failed to reach.

### 6.5 CLI and exit codes

```
python sheet_to_neon.py                  # gid 1987728467
python sheet_to_neon.py --gid 123456789  # another tab
python sheet_to_neon.py --dry-run        # read, coerce, report; touch nothing
python sheet_to_neon.py --prune          # also delete rows the tab no longer has
```

`--dry-run` still requires the sheet but never opens a connection, and prints
the tab title, row count, how many rows coerced to a usable `order_number`, and
the `created_date` range. It is the cheap way to confirm *which month* gid
1987728467 actually is.

Exit 0 only on a completed run. 1 on: `DATABASE_URL` unset, missing
`service_account.json`, gid not found, empty tab, or any write failure. The
summary line prints rows read, written, pruned, and the tab title.

## 7. Operating it

### 7.1 First run

```
python -m sql.checksum                       # 001 unchanged: expected == header
psql "$DATABASE_URL" -f sql/002_unifi_sheet_orders.sql   # or: python apply_schema.py
python sheet_to_neon.py --dry-run
python sheet_to_neon.py
```

Under cron, hourly at most — a Neon compute waking for a 2,000-row upsert is
cheap, but the tab is only rewritten by the nightly scrape.

### 7.2 Two queries the provenance columns buy

Rows the last run did not see (deleted from the tab, or the run died early):

```sql
SELECT order_number, source_tab, imported_at
  FROM unifi_sheet_orders
 WHERE source_gid = 1987728467
   AND imported_at < (SELECT max(imported_at) FROM unifi_sheet_orders WHERE source_gid = 1987728467);
```

Where the mirror and the scraper's own table disagree — the reason the column
names were kept identical:

```sql
SELECT s.order_number, s.order_status, o.order_status, s.status, o.status
  FROM unifi_sheet_orders s
  JOIN unifi_orders o USING (order_number)
 WHERE s.order_status IS DISTINCT FROM o.order_status
    OR s.status       IS DISTINCT FROM o.status;
```

### 7.3 Environment

```
# Optional. Defaults are the "Unifi Orders" spreadsheet and the tab from
# the gid=1987728467 link; set these only to point the importer elsewhere.
SHEET_IMPORT_SPREADSHEET_ID=
SHEET_IMPORT_GID=
```

`DATABASE_URL` and `service_account.json` are the existing ones. No new
credentials.

## 8. Testing

`tests/conftest.py`'s `db` fixture applies `001` then `002` — in that order, so
the role exists before `002`'s grants — and every existing test keeps working
unchanged.

`tests/test_sheet_to_neon.py`, against a throwaway database via
`TEST_DATABASE_URL`, with a `FakeWorksheet` in the shape `tests/test_backfill.py`
already uses (`.get_all_values()`, `.title`, plus an `.id`):

1. **Round trip.** A two-row fake tab lands two rows with the right typed
   values: `created_date` parsed as `Asia/Kuala_Lumpur`, `"-"` status stored as
   NULL, the `" - "` appointment range parsed to its start while `raw` keeps the
   full text.
2. **Idempotent.** Running twice leaves two rows, and every column but
   `imported_at` is byte-identical. This is the test that would catch someone
   adding a trigger or a `bigserial` later.
3. **Provenance.** `source_gid`, `source_tab` and `source_row` match the fake
   worksheet; `source_row` is 2 for the first data row.
4. **Changed cell wins.** Re-running with an edited `Order Status` overwrites;
   re-running with a *cleared* cell writes NULL rather than keeping the old
   value.
5. **Blank `Last Synced` stays NULL** — the §6.3 deviation, which a later
   "simplification" back to bare `coerce_row()` would silently undo.
6. **Short and long rows.** Padded and logged respectively (already covered for
   `rows_from_values`; asserted here end to end).
7. **`--prune`** removes a row dropped from the tab and leaves other gids alone.
8. **No `DATABASE_URL` exits 1** and writes nothing.
9. **Missing gid exits 1** and lists the available titles.
10. **`unifi_orders` is untouched** by a full import — count stays 0, and
    `unifi_order_status_events` stays empty. The point of §2.1, asserted.
11. **`001` is byte-stable**: `sql.checksum.compute()` still equals its header
    (the existing `tests/test_checksum.py` covers this; noted because `002`
    landing must not change it).

## 9. Out of scope

- Resolving *which month* gid 1987728467 is. `--dry-run` answers it in one
  command; nothing in the design depends on the answer.
- Writing back to the sheet. This is read-only on the Google side.
- Any change to `unifi_orders`, the status trigger, the views, or
  `writers.py`'s dual-write.
- Portal or Prisma work. The portal has no reason to know this table exists;
  if that changes, `002` gets mirrored the way `001` was.
- Multi-tab import. `--gid` takes one. `backfill_neon.py` already walks all
  month tabs into `unifi_orders`.
