# Spec — Unifi scraper data in the BizzFlow admin panel

Status: **draft for approval — not implemented**
Target repo: `jobhuntersaipro-design/wifibizz_bill_generator` (drop this file in `context/features/`)
Source repo: `jobhuntersaipro-design/unifi-scraper` (Flask service + Python scrapers)

Three new pages under `/admin`, reading Unifi order data that the scraper writes
into Neon, plus a "Sync now" control that drives the scraper from the portal.

---

## 1. What this replaces

Today the pipeline is: Playwright scraper → Google Sheets (one tab per month) →
n8n workflow → Telegram message. The sheet is the database, the n8n Code node is
the query engine, and Telegram is the only UI.

After this spec: scraper → **Neon** → portal reads it. Telegram becomes one more
reader of the same tables rather than a parallel pipeline that recomputes
everything from spreadsheet cells.

---

## 2. Decisions already made

| Question | Decision |
|---|---|
| Rover List ownership | **Portal is the source of truth.** Full CRUD in `/admin`; the Google Sheet is retired after a one-time import. |
| PII (IC number, phone, email, address) | **Shown in full** to anyone who can reach `/admin`. No masking in v1. |
| Sync | **"Sync now" button** in the portal, which requires locking down the scraper's Flask API first (§7). |
| Where the tables live | Same Neon database as the portal, `public` schema, `unifi_`-prefixed table names, managed by Prisma migrations (§4.1). |

**One caveat on PII, stated once and then dropped:** `/admin` is a single shared
JWT (`BIZZFLOW_ADMIN_USERNAME` + `BIZZFLOW_ADMIN_PWD`, 8h cookie) — there is no
per-person identity behind it, so the audit trail can only ever say "admin".
That is acceptable while `/admin` is you alone. If a second person ever gets the
password, revisit. The spec keeps every PII column behind a single
`src/lib/unifi-pii.ts` selector so masking can be added later by changing one
file rather than six components.

---

## 3. Open questions (blocking the parts they touch)

**Answered 2026-09-20** (see `docs/superpowers/specs/2026-09-20-unifi-neon-writer-design.md`):

1. ~~**Order volume**~~ — **over 2,000 orders/month.** The §5 pagination and
   index plan stands as written; the load-everything pattern of `OrderOversight`
   is ruled out.
4. ~~**Same Neon branch**~~ — **yes, same project and branch**, but the scraper
   connects as its own `unifi_scraper` role granted only on the `unifi_*` tables,
   not with the portal's connection string. The scraper runs on a droplet and its
   credentials sit on disk; a compromise there should not reach the portal's own
   data.

Also decided: **the DDL is canonical in the scraper repo** (`sql/001_unifi_schema.sql`),
and the portal's hand-written Prisma migration is a checksum-verified copy of it,
registered with `prisma migrate resolve`. This reverses §4.1's implied ownership
so that the scraper can start filling Neon before any portal page exists.

Still open:

2. **Where does the Flask scraper run, and is it reachable from Vercel?** The
   order-entry scraper is a droplet at `scraper.bizzflow.top`. Is `api_server.py`
   on the same box (different port) or somewhere else? "Sync now" needs a public
   HTTPS URL that Vercel can reach.
3. **The n8n Telegram workflow will break** when the Rover List sheet stops being
   maintained — it reads that sheet directly. Do we (a) repoint n8n at Neon in
   the same change, (b) keep writing the sheet as a read-only mirror during a
   transition, or (c) retire the workflow and send Telegram from the portal?
5. **Timeline retention** — keep status events forever, or prune after N months?
   Forever is my default (they're small and they're the audit trail).

---

## 4. Data layer

### 4.1 Where the tables go

Prisma models in the existing `prisma/schema.prisma`, `public` schema, table
names prefixed `unifi_` via `@@map`. **Not** a separate Postgres schema: the
project has no `multiSchema` preview feature enabled and one `datasource`, and
adding a second schema would mean touching the generator config, the migration
runner (`scripts/migrate-deploy.mjs`) and the Neon adapter setup for no benefit
the prefix doesn't already give.

Per `context/coding-standard.md`: `prisma migrate dev` for every change, never
`db push`; `prisma migrate status` before committing.

### 4.2 Models

```prisma
/// One Unifi sales channel — a rover (van), KFA counter, affiliate or reseller.
/// `fleetLabel` is what people call it ("CAR 1", "JEM 3"); it was the `name_2`
/// column of the Rover List sheet, and it is what the Telegram summary prints.
model UnifiChannel {
  channelCode     String   @id @map("channel_code")          // = Order.orgCode, e.g. "RV10551"
  channelName     String   @map("channel_name")
  plate           String?                                     // was name_1
  fleetLabel      String?  @map("fleet_label")                // was name_2
  channelCategory String?  @map("channel_category")
  channelType     String?  @map("channel_type")               // Rover | KFA | AFFILAITE | Reseller Agent
  active          Boolean  @default(true)
  /// Generated column, see migration: coalesce(fleet_label, plate, channel_name).
  /// Read-only from Prisma's point of view — never write it.
  displayName     String   @map("display_name")
  createdAt       DateTime @default(now()) @map("created_at")
  updatedAt       DateTime @updatedAt @map("updated_at")

  @@map("unifi_channels")
}

model UnifiOrder {
  orderNumber       String    @id @map("order_number")
  eventType         String?   @map("event_type")
  orderStatus       String?   @map("order_status")
  createdDate       DateTime? @map("created_date")
  updatedDate       DateTime? @map("updated_date")
  orgCode           String?   @map("org_code")                // NOT a foreign key — see below
  organizationName  String?   @map("organization_name")
  customerName      String?   @map("customer_name")
  companyName       String?   @map("company_name")
  email             String?
  phoneNumber       String?   @map("phone_number")
  appointmentDate   DateTime? @map("appointment_date")
  address           String?
  package           String?
  device            String?
  icNumber          String?   @map("ic_number")
  creator           String?
  custId            String?   @map("cust_id")
  status            String?                                   // installation status
  statusLatestDate  DateTime? @map("status_latest_date")
  statusScrapeDate  DateTime? @map("status_scrape_date")      // when we last VERIFIED
  lastSynced        DateTime  @default(now()) @map("last_synced")
  raw               Json?

  @@index([updatedDate(sort: Desc)])
  @@index([orgCode])
  @@index([orderStatus])
  @@index([status])
  @@map("unifi_orders")
}

/// Append-only. One row per OBSERVED CHANGE, never one per check.
model UnifiOrderStatusEvent {
  id               BigInt    @id @default(autoincrement())
  orderNumber      String    @map("order_number")
  orderStatus      String?   @map("order_status")
  status           String?
  statusLatestDate DateTime? @map("status_latest_date")
  custId           String?   @map("cust_id")
  prevOrderStatus  String?   @map("prev_order_status")
  prevStatus       String?   @map("prev_status")
  changedAt        DateTime  @default(now()) @map("changed_at")
  scrapeRunId      BigInt?   @map("scrape_run_id")

  @@index([orderNumber, changedAt(sort: Desc)])
  @@index([changedAt(sort: Desc)])
  @@map("unifi_order_status_events")
}

model UnifiScrapeRun {
  id              BigInt    @id @default(autoincrement())
  jobId           String?   @map("job_id")
  monthText       String?   @map("month_text")
  year            Int?
  scrapeMode      String?   @map("scrape_mode")
  triggeredBy     String?   @map("triggered_by")      // "cron" | "admin"
  startedAt       DateTime  @default(now()) @map("started_at")
  finishedAt      DateTime? @map("finished_at")
  status          String?                              // running | done | error
  ordersProcessed Int?      @map("orders_processed")
  successful      Int?
  skipped         Int?
  failed          Int?
  error           String?

  @@index([startedAt(sort: Desc)])
  @@map("unifi_scrape_runs")
}
```

**`orgCode` is deliberately not a foreign key.** A new rover appears in the Unifi
portal before anyone adds it to the fleet list; with an FK that order fails to
insert and the data is lost. Without one it lands, and the Rover List page shows
it in the "Unmapped" panel (§6.3) as work to do.

### 4.3 What Prisma can't express — one hand-written migration

Generate with `prisma migrate dev --create-only`, then add to the SQL file:

- `unifi_channels.display_name` as `GENERATED ALWAYS AS (coalesce(nullif(fleet_label,''), nullif(plate,''), channel_name)) STORED` — the fallback chain enforced by the database, not by a TypeScript expression that can drift.
- The `unifi_log_order_status_change()` trigger on `unifi_orders` (**renamed 2026-09-20** — `public` already contains the portal's own `order_status_events` table and, in all likelihood, a `log_order_status_change()` behind it. Trigger functions are schema-scoped, so the unprefixed name would collide with or silently replace it. Every object this schema creates carries the `unifi_` prefix, not just the tables.) (full body in the scraper repo's migration; `AFTER INSERT OR UPDATE ... FOR EACH ROW`). Putting it in the DB means `check_status.py`, the backfill scripts and any manual `UPDATE` all log correctly — the portal does not have to remember.
- Views `unifi_order_status_timeline`, `unifi_monthly_stats`, `unifi_monthly_channel_breakdown`, `unifi_unmapped_channels`.

Views are read via `prisma.$queryRaw` with a Zod-parsed row type (no `views`
preview feature). Each view gets one typed reader in `src/lib/unifi-queries.ts`
so the raw SQL exists in exactly one place.

---

## 5. Page 1 — Unifi Orders

**Route:** `/admin/unifi/orders`, detail at `/admin/unifi/orders/[orderNumber]`

> **Naming matters here.** `/admin/orders` already exists and means something
> completely different — orders BizzFlow agents key in for submission. These are
> orders scraped back OUT of the Unifi dealer portal. Everything new lives under
> `/admin/unifi/*` and is labelled "Unifi Orders" in the nav, never "Orders".

**Structure** (house pattern): thin server page → `<UnifiOrders />` client
component in `src/components/admin/unifi-orders.tsx` → server actions in
`src/actions/admin-unifi.ts`, each starting with `requireAdmin()` and returning
`{ success, data, error }`.

**Server-side pagination**, unlike `OrderOversight` which loads every order and
filters on the client. That pattern is fine for hundreds of agent orders; it is
not fine for years of scraped orders. `adminListUnifiOrders` takes
`{ page, perPage, q, orderStatus, status, orgCode, from, to, sort }` and returns
`{ rows, total }`. Reuse `PAGE_SIZES` / `pageCount` / `pageRangeLabel` from
`src/lib/paginate.ts` for the controls; the slicing happens in SQL.

**Stat tiles** (from `unifi_monthly_stats` for the selected range): Total,
Completed + %, Cancelled + %, Other. Same definitions as the Telegram message so
the two never disagree:
- `completed` = `lower(order_status) = 'completed'` (exact)
- `cancelled` = `order_status` matching `cancel|void|failed` (substring)
- Everything else counts toward Total only.

**Columns:** Order Number · Created · Updated · Channel · Package · Device ·
Order Status · Install Status · Cust ID · Customer · Phone · Last Synced.
Channel renders as `displayName` with `orgCode` beneath it in muted text —
`RV10551 | CAR 1` as two lines rather than the Telegram string.

**Filters:** month (defaults to current), order status, install status, channel
(searchable select over active channels), date range on Updated, and free text
`q` matching order number / customer / phone / IC / cust id / address.

**Row → detail page:** every scraped field, the full status timeline for that
order (§7), and a collapsed raw-JSON viewer of `raw`.

**CSV export** of the current filter set via `toCsv` from `src/lib/admin-search.ts`.

**States:** loading skeleton, empty ("No orders match these filters"), error with
retry. Mobile: the table collapses to cards — the admin shell's sidebar is a
drawer on mobile and a 12-column table is unusable there.

---

## 6. Page 2 — Rover List

**Route:** `/admin/unifi/rovers` · **Component:** `src/components/admin/unifi-rovers.tsx`

### 6.1 Table
Columns: Channel Code · Channel Name · Plate · **Fleet Label** · Category · Type ·
Active · Shows as (`displayName`, read-only, explained in a tooltip as
"fleet label, else plate, else channel name").

Filters: search across all name fields, type, active/inactive (default: active).

### 6.2 Create / edit
shadcn dialog, Zod-validated, `adminUpsertChannel`:
- `channelCode` — required, trimmed, uppercased, **immutable after create** (it is the join key to orders; editing it silently reassigns history). Editing shows it disabled with a note.
- `channelName` — required.
- `plate`, `fleetLabel`, `channelCategory` — optional.
- `channelType` — select: `Rover | KFA | AFFILAITE | Reseller Agent` (free text allowed; the existing sheet has the "AFFILAITE" typo and the import must not silently "fix" it, or codes stop matching).
- `active` — toggle.

**Deactivate, don't delete.** Orders reference `orgCode` and deactivating keeps
the label resolving for historical rows. A hard delete is offered only when zero
orders reference the code, and asks for confirmation.

Every mutation writes an audit row via the existing `recordAudit` — add
`channel_created`, `channel_updated`, `channel_deactivated`, `channel_deleted`,
`channels_imported` to the `AuditAction` union in `src/lib/audit.ts`.

### 6.3 Unmapped channels panel
Above the table, driven by `unifi_unmapped_channels`: org codes that appear on
orders but have no channel row, with the organization name we saw and an order
count. Each has an "Add to fleet" button that opens the create dialog prefilled.
This is the queue that today is invisible — those orders currently print in
Telegram as `RV10xxx | <Organization Name>` and nobody notices.

### 6.4 One-time import from the sheet
Admin-only action `adminImportChannelsFromSheet()`, run once at cutover, using
the existing `src/lib/google-sheets.ts` + `GOOGLE_SERVICE_ACCOUNT_JSON`.

Source: spreadsheet `1gIgZdLRvF0akA2kxom1CLbpl2NtBj1HsHMtX22Mq4tA`, `Sheet1`.
Column mapping — note `name_1`/`name_2` are the sheet's literal headers:

| Sheet column | Field |
|---|---|
| `Channel Code` | `channelCode` (trim) |
| `Channel Name` | `channelName` |
| `name_1` | `plate` |
| `name_2` | `fleetLabel` |
| `Channel Category` | `channelCategory` |
| `Channel Type` | `channelType` |

Idempotent upsert on `channelCode`; empty strings become `null`; rows with a
blank code are skipped and reported. Returns `{ created, updated, skipped }` and
shows it in a toast. After a verified import, **the button is removed in the same
PR that retires the sheet** — leaving a one-way sheet→DB import wired up next to
a DB that is now authoritative is how you get a Monday morning where someone's
edits vanish.

---

## 7. Page 3 — Status timeline

**Route:** `/admin/unifi/timeline` · reads `unifi_order_status_timeline`.

Two views of the same append-only log:

**Feed** — most recent transitions first: when, order number, channel,
`prevStatus → status` as a pill pair, how long the previous state held, and the
cust-id swap if one happened on the same event. Filters: date range, from-status,
to-status, channel, order number. Paginated server-side.

**Churn summary** — a transition matrix for the period (`prevStatus` × `status`
with counts), which is the question this whole table exists to answer:
*how many Active orders went Terminated last month?* Today that is unanswerable,
because Sheets overwrites the cell.

**Per-order** — a vertical stepper on the order detail page: each state, when it
started, how long it held, open state last. `prevStatus IS NULL` renders as
"State at migration", not as a transition — history begins at cutover and the UI
must not imply otherwise.

**This page is read-only, permanently.** No edit, no delete, no "correct this
event" affordance anywhere. Events are what we observed; if an observation was
wrong, the fix is a new observation.

---

## 8. Sync now

### 8.1 Portal side
A "Sync now" button in the Unifi Orders header, beside a banner reading
`Last synced 14 minutes ago · 412 orders · 3 failed` from the latest
`unifi_scrape_runs` row (with a red state when the last run errored).

Clicking it opens a small popover: month + year (defaults to current) and mode
(incremental / full). `adminTriggerUnifiSync` then:
1. `requireAdmin()`
2. rate-limits via the existing `src/lib/rate-limit.ts` (Upstash) — **1 sync per 5 minutes**, since a scrape drives a real browser session against the dealer portal
3. `POST ${UNIFI_SCRAPER_API_URL}/jobs` with `X-Internal-Token`, `AbortSignal.timeout(10_000)` and `cache: "no-store"` — mirroring `src/lib/order-start.ts`
4. inserts a `unifi_scrape_runs` row with `triggeredBy: "admin"`, `status: "running"`
5. returns the job id; the button becomes a live "Syncing…" state

> **Corrected 2026-09-20 against the actual `api_server.py`.** An earlier draft of
> this section named `POST /scrape` and a poll of `/status/<job_id>`. Neither is
> right. `/scrape` is *blocking* — it returns only when the scrape finishes, so a
> 10-second abort would always fire. The async job API is `POST /jobs` →
> `GET /jobs/<job_id>`, and `/status` is an unrelated summary endpoint that takes
> no job id.

Two further facts about the lock, both differing from what this section assumed:
`scrape_locks` is defined twice in `api_server.py` (lines 24 and 297), the second
definition silently discarding anything held by the first; and `POST /jobs`
enforces a **global** single-job lock, not a per-month one, so it rejects a new
job while *any* month is running. Until that is fixed, a busy response cannot
honestly say "A sync for Sep 2026 is already running" — it means "a sync is
already running". A busy response is a toast, not an error.

### 8.2 Completion
`POST /api/hooks/unifi-scraper`, modelled exactly on the existing
`/api/hooks/scraper`: refuse with 503 when the secret is unset, 401 on mismatch,
then update the `unifi_scrape_runs` row and `revalidatePath("/admin/unifi/orders")`.
A poll of `/status/<job_id>` is the fallback while the tab is open.

### 8.3 Securing `api_server.py` — prerequisite, in the scraper repo
`api_server.py` is today documented as *"Flask API server for open scraping - NO
AUTHORIZATION — Accessible to everyone"*. Exposing that to the internet so Vercel
can call it would let anyone trigger dealer-portal scrapes, download the CSV of
customer data, or overwrite stored credentials via `/save_credentials`.

Required before the button ships:
1. A `@require_token` decorator checking `X-Internal-Token` against
   `UNIFI_SCRAPER_API_TOKEN`, applied to **every** route except `/health`;
   refuse with 503 when the env var is unset (same posture as the webhook route).
2. `/save_credentials` additionally restricted or removed — it writes the
   encrypted credential store.
3. ~~CORS limited to the portal origin.~~ **Dropped** — there is no CORS
   handling in the scraper and `flask-cors` is not a dependency. A Vercel server
   action is server-to-server and sends no browser `Origin`, so this would
   protect nothing. The exposure is the missing authentication, covered by item 1.

### 8.4 New environment variables
```
# Portal (.env.local + Vercel) and scraper (.env on the host) — must match
UNIFI_SCRAPER_API_URL=https://<host>            # portal only
UNIFI_SCRAPER_API_TOKEN=                        # both
UNIFI_SCRAPER_WEBHOOK_SECRET=                   # both
BIZZFLOW_WEBHOOK_URL=.../api/hooks/unifi-scraper  # scraper only
```
Add to `.env.example` with the same commented style as the existing entries.

---

## 9. Navigation wiring

Both of these, or the topbar mislabels the new pages:
1. `navItems` in `src/components/admin/sidebar.tsx` — one "Unifi" group with
   Orders / Rover List / Timeline, placed after "Orders".
2. `src/lib/admin-nav.ts` — exact matches for the three routes, a
   `/admin/unifi/orders` prefix entry in `SECTIONS`, and a back target of
   `/admin/unifi/orders` for the detail page. Its own doc comment says an
   unmapped page gets labelled confidently and wrongly.

---

## 10. Out of scope for v1

PII masking and reveal-audit · per-person admin identities · editing scraped
order data from the portal (read-only; the dealer portal owns it) · sending
Telegram from the portal · charts beyond the stat tiles and the churn matrix ·
the n8n repoint (question 3).

---

## 11. Acceptance criteria

1. `prisma migrate status` clean; the trigger and all four views exist on a fresh DB created by migrations alone.
2. Inserting an order then updating its `status` produces exactly **two** event rows; re-running the same update with no change produces **none**.
3. `unifi_channels.display_name` returns the fleet label, and falls back correctly for the sheet's blank rows (`RV10558`, `AF10111`, `RAGT10896` all resolve to their channel name, not to an empty string).
4. The Unifi Orders stat tiles match the current Telegram message for the same month, to the row.
5. A channel edited in the portal changes the label shown on existing orders immediately, with an audit row written.
6. An org code present on orders but absent from channels appears in the Unmapped panel.
7. "Sync now" is rejected with 401 when the token is wrong, rate-limited on a second click within 5 minutes, and the banner reflects the run's outcome without a manual refresh.
8. Every new server action refuses without an admin cookie.
9. `npm run lint`, `npm run test`, `npm run build` pass; no `any`.

---

## 12. Build order

1. **Schema + writer in the scraper repo** — `sql/001_unifi_schema.sql` (canonical
   DDL) and `neon_writer.py` dual-writing to Sheets and Neon, plus a one-time
   backfill of every month tab. Verifiable by criteria 1–3. Designed in
   `docs/superpowers/specs/2026-09-20-unifi-neon-writer-design.md` in that repo.
2. **Prisma models + migration in the portal** — a checksum-verified copy of the
   SQL above, registered with `prisma migrate resolve`. Run the dual-write a week
   and compare before anything reads from Neon.
3. **Page 1 read-only** (§5) — the moment the sheet stops being the UI.
4. **Page 2 + import** (§6) — flip the source of truth; answer question 3 before this lands.
5. **Page 3** (§7).
6. **Lock down the Flask API, then Sync now** (§8) — in that order, never the reverse.
