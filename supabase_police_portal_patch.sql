-- ============================================================
-- TrafficMonitor AI — Supabase Patch (Police Portal)
-- Date: 2026-05-03
--
-- Purpose
-- - Ensure the schema supports police-role logins and the /police/alerts portal.
-- - Safe to run multiple times (idempotent guards where possible).
--
-- How to use
-- 1) Open Supabase Dashboard → SQL Editor
-- 2) Paste and run this file.
-- ============================================================

BEGIN;

-- 1) Ensure enum value exists (if you already created user_role earlier without 'police')
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_role') THEN
    IF NOT EXISTS (
      SELECT 1
      FROM pg_enum e
      JOIN pg_type t ON t.oid = e.enumtypid
      WHERE t.typname = 'user_role' AND e.enumlabel = 'police'
    ) THEN
      ALTER TYPE user_role ADD VALUE 'police';
    END IF;
  END IF;
END $$;

-- 2) Users: ensure police_station_id exists and is linked to police_stations
ALTER TABLE IF EXISTS public.users
  ADD COLUMN IF NOT EXISTS police_station_id BIGINT;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_users_police_station') THEN
    -- Only add the FK if both tables exist
    IF EXISTS (
      SELECT 1
      FROM information_schema.tables
      WHERE table_schema = 'public' AND table_name = 'police_stations'
    ) AND EXISTS (
      SELECT 1
      FROM information_schema.tables
      WHERE table_schema = 'public' AND table_name = 'users'
    ) THEN
      ALTER TABLE public.users
        ADD CONSTRAINT fk_users_police_station
        FOREIGN KEY (police_station_id)
        REFERENCES public.police_stations (id)
        ON DELETE SET NULL;
    END IF;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_users_police_station_id ON public.users (police_station_id);

-- 3) Alerts: add fields that the police portal can render
-- Your current app stores vehicle_info as JSON/text; this extra column is optional but harmless.
ALTER TABLE IF EXISTS public.alert_notifications
  ADD COLUMN IF NOT EXISTS detected_person_name TEXT;

-- Optional: if you want to store confidence later
ALTER TABLE IF EXISTS public.alert_notifications
  ADD COLUMN IF NOT EXISTS detected_name_confidence REAL;

-- Helpful indexes for portal queries (station filter + recent alerts)
CREATE INDEX IF NOT EXISTS idx_alert_station_created_at
  ON public.alert_notifications (police_station_id, created_at DESC);

COMMIT;

-- Notes
-- - The app code currently filters police alerts by users.police_station_id when present.
-- - If your existing Supabase schema already matches supabase_migration.sql, this patch will be mostly a no-op.
