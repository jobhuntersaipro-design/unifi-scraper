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
