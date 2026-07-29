-- ============================================================
-- Repo: whatsapp-lead-automation
-- Run this in Supabase → SQL Editor (once), AFTER the
-- prospect-uploader repo's supabase/schema.sql has already been run
-- (this file assumes the `prospects` table already exists).
-- ============================================================

-- 1. Clients receiving leads
CREATE TABLE IF NOT EXISTS clients (
    client_id       SERIAL PRIMARY KEY,
    client_name     TEXT NOT NULL,
    whatsapp_number TEXT NOT NULL UNIQUE,   -- E.164 format e.g. +919876543210
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. Extend `prospects` (created by the other repo) with distribution state.
--    Kept separate from `outreach_status` so your own cold-outreach tracking
--    never collides with what the automation has sent to clients.
ALTER TABLE prospects
    ADD COLUMN IF NOT EXISTS assigned_client_id INTEGER REFERENCES clients(client_id),
    ADD COLUMN IF NOT EXISTS dist_status TEXT NOT NULL DEFAULT 'AVAILABLE'
        CHECK (dist_status IN ('AVAILABLE', 'SENT')),
    ADD COLUMN IF NOT EXISTS dist_sent_at TIMESTAMPTZ;

-- A prospect can only ever be marked SENT once — enforced at the DB level,
-- not just in application code.
CREATE UNIQUE INDEX IF NOT EXISTS idx_prospects_single_assignment
    ON prospects (prospect_id) WHERE dist_status = 'SENT';

CREATE INDEX IF NOT EXISTS idx_prospects_dist_status ON prospects (dist_status);
CREATE INDEX IF NOT EXISTS idx_prospects_imported_at ON prospects (imported_at);

-- 3. Audit trail of every send attempt
CREATE TABLE IF NOT EXISTS send_log (
    log_id          SERIAL PRIMARY KEY,
    client_id       INTEGER REFERENCES clients(client_id),
    client_name     TEXT NOT NULL,
    prospect_id     INTEGER REFERENCES prospects(prospect_id),
    whatsapp_number TEXT NOT NULL,
    sent_time       TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL CHECK (status IN ('SUCCESS', 'FAILED')),
    attempt_number  INTEGER NOT NULL DEFAULT 1,
    error_message   TEXT
);

-- 4. Daily send counter — resets itself automatically because a new
--    calendar date simply has no row yet (no midnight cron job needed).
CREATE TABLE IF NOT EXISTS daily_send_counter (
    send_date   DATE PRIMARY KEY,
    sent_count  INTEGER NOT NULL DEFAULT 0
);

CREATE OR REPLACE VIEW v_today_sent_count AS
SELECT COALESCE(sent_count, 0) AS sent_today
FROM daily_send_counter
WHERE send_date = CURRENT_DATE;

-- NOTE: `clients`, `send_log`, and `daily_send_counter` don't need RLS
-- policies for the anon key — the automation script (distribute_leads.py)
-- connects with the direct Postgres connection string (DATABASE_URL secret),
-- which bypasses RLS entirely. RLS above only matters for the browser
-- upload page's anon-key access to `prospects`.
