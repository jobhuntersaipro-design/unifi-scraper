-- schema-checksum: be736dde6df4098ed9e023235fe97ef5ae09ead9d07576b7d44fa442928951c9
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
    coalesce(c.display_name, nullif(o.organization_name, ''), nullif(o.org_code, '')) AS channel_display_name,
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

-- Grouped by month + org_code ONLY (not by channel_display_name): for an
-- unmapped code the label falls back to organization_name, which is
-- scraped text and can vary in case/whitespace within a month. Grouping
-- on the label too would split one rover's counts across several rows.
-- max(...) collapses whichever variant the label expression turns up.
CREATE OR REPLACE VIEW unifi_monthly_channel_breakdown AS
SELECT
    date_trunc('month', o.created_date) AS month,
    o.org_code,
    max(coalesce(c.display_name, nullif(o.organization_name, ''), nullif(o.org_code, ''))) AS channel_display_name,
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
GROUP BY 1, 2;

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
