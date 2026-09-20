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
