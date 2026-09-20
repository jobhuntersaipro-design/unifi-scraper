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
