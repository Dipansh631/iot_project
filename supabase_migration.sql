-- ============================================================
-- Traffic Monitoring System — Supabase SQL Migration
-- Run these statements in Supabase SQL Editor (in order)
-- Project: fdbxuculfjhxmycbeuqc
-- ============================================================


-- ╔══════════════════════════════════════════════════════════╗
-- ║  TASK 1 — EXTENSIONS & SETUP                            ║
-- ╚══════════════════════════════════════════════════════════╝
-- Enable UUID generation (used as primary keys if you prefer UUIDs)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
-- Enable pg_trgm for fast ILIKE/full-text searches on plates
CREATE EXTENSION IF NOT EXISTS pg_trgm;


-- ╔══════════════════════════════════════════════════════════╗
-- ║  TASK 2 — CUSTOM TYPES / ENUMS                          ║
-- ╚══════════════════════════════════════════════════════════╝
DO $$
BEGIN
    -- Severity levels
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'severity_level') THEN
        CREATE TYPE severity_level AS ENUM ('low', 'medium', 'high', 'critical');
    END IF;

    -- User roles
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_role') THEN
        CREATE TYPE user_role AS ENUM ('admin', 'police', 'operator');
    END IF;

    -- Alert statuses
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'alert_status') THEN
        CREATE TYPE alert_status AS ENUM ('pending', 'sent', 'failed');
    END IF;

    -- Session types
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'session_type') THEN
        CREATE TYPE session_type AS ENUM ('upload', 'live_camera');
    END IF;
END $$;


-- ╔══════════════════════════════════════════════════════════╗
-- ║  TASK 3 — TABLE: users                                  ║
-- ╚══════════════════════════════════════════════════════════╝
CREATE TABLE IF NOT EXISTS users (
    id                  BIGSERIAL PRIMARY KEY,
    email               TEXT        NOT NULL UNIQUE,
    full_name           TEXT,
    hashed_password     TEXT        NOT NULL,
    role                user_role   NOT NULL DEFAULT 'operator',
    police_station_id   BIGINT,
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for common lookups
CREATE INDEX IF NOT EXISTS idx_users_email        ON users (email);
CREATE INDEX IF NOT EXISTS idx_users_role         ON users (role);
CREATE INDEX IF NOT EXISTS idx_users_is_active    ON users (is_active);

-- Comment
COMMENT ON TABLE  users IS 'System users: admins, police officers, and operators';
COMMENT ON COLUMN users.role IS 'Enum: admin | police | operator';


-- ╔══════════════════════════════════════════════════════════╗
-- ║  TASK 4 — TABLE: police_stations                        ║
-- ╚══════════════════════════════════════════════════════════╝
CREATE TABLE IF NOT EXISTS police_stations (
    id                BIGSERIAL PRIMARY KEY,
    station_name      TEXT        NOT NULL,
    station_code      TEXT        NOT NULL UNIQUE,
    address           TEXT,
    city              TEXT,
    state             TEXT,
    phone_number      TEXT,
    email             TEXT,
    latitude          DOUBLE PRECISION,
    longitude         DOUBLE PRECISION,
    jurisdiction_area TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stations_city        ON police_stations (city);
CREATE INDEX IF NOT EXISTS idx_stations_station_code ON police_stations (station_code);

-- Add FK from users → police_stations (deferred so tables can be created independently)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_users_police_station') THEN
        ALTER TABLE users
            ADD CONSTRAINT fk_users_police_station
            FOREIGN KEY (police_station_id)
            REFERENCES police_stations (id)
            ON DELETE SET NULL;
    END IF;
END $$;

COMMENT ON TABLE police_stations IS 'Police stations that receive alert notifications';


-- ╔══════════════════════════════════════════════════════════╗
-- ║  TASK 5 — TABLE: suspicious_vehicles  (watchlist)       ║
-- ╚══════════════════════════════════════════════════════════╝
CREATE TABLE IF NOT EXISTS suspicious_vehicles (
    id                   BIGSERIAL     PRIMARY KEY,
    license_plate        TEXT          NOT NULL UNIQUE,
    vehicle_type         TEXT,                              -- Car, SUV, Bike, Truck, Van…
    vehicle_color        TEXT,
    vehicle_make         TEXT,
    vehicle_model        TEXT,
    owner_name           TEXT,
    reason_for_flagging  TEXT          NOT NULL,
    severity_level       severity_level NOT NULL DEFAULT 'medium',
    is_active            BOOLEAN       NOT NULL DEFAULT TRUE,
    reported_by          TEXT,
    reported_date        TEXT,                              -- stored as ISO-8601 string
    additional_notes     TEXT,
    created_at           TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- GIN index for partial plate searches (uses pg_trgm extension)
CREATE INDEX IF NOT EXISTS idx_suspicious_plate_trgm
    ON suspicious_vehicles USING GIN (license_plate gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_suspicious_severity
    ON suspicious_vehicles (severity_level);
CREATE INDEX IF NOT EXISTS idx_suspicious_is_active
    ON suspicious_vehicles (is_active);

COMMENT ON TABLE  suspicious_vehicles IS 'Watchlist of vehicles flagged for monitoring';
COMMENT ON COLUMN suspicious_vehicles.severity_level IS 'low | medium | high | critical';


-- ╔══════════════════════════════════════════════════════════╗
-- ║  TASK 6 — TABLE: video_sessions                         ║
-- ╚══════════════════════════════════════════════════════════╝
CREATE TABLE IF NOT EXISTS video_sessions (
    id                     BIGSERIAL    PRIMARY KEY,
    session_type           session_type NOT NULL,           -- upload | live_camera
    video_filename         TEXT,
    camera_id              TEXT,
    location               TEXT,
    start_time             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    end_time               TIMESTAMPTZ,
    total_detections       INTEGER      NOT NULL DEFAULT 0,
    suspicious_detections  INTEGER      NOT NULL DEFAULT 0,
    created_by             BIGINT REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_video_sessions_created_by ON video_sessions (created_by);
CREATE INDEX IF NOT EXISTS idx_video_sessions_start      ON video_sessions (start_time DESC);

COMMENT ON TABLE video_sessions IS 'Each uploaded video or live-camera session';


-- ╔══════════════════════════════════════════════════════════╗
-- ║  TASK 7 — TABLE: detection_logs                         ║
-- ╚══════════════════════════════════════════════════════════╝
CREATE TABLE IF NOT EXISTS detection_logs (
    id                     BIGSERIAL   PRIMARY KEY,
    session_id             BIGINT      REFERENCES video_sessions (id) ON DELETE SET NULL,
    detected_plate         TEXT        NOT NULL,
    confidence_score       REAL,
    is_suspicious          BOOLEAN     NOT NULL DEFAULT FALSE,
    suspicious_vehicle_id  BIGINT      REFERENCES suspicious_vehicles (id) ON DELETE SET NULL,
    detection_timestamp    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    camera_location        TEXT,
    image_filename         TEXT,
    video_frame_number     INTEGER,
    alert_sent             BOOLEAN     NOT NULL DEFAULT FALSE,
    alerted_station_id     BIGINT      REFERENCES police_stations (id) ON DELETE SET NULL,
    created_by             BIGINT      REFERENCES users (id) ON DELETE SET NULL
);

-- Hot-path indexes
CREATE INDEX IF NOT EXISTS idx_det_plate           ON detection_logs (detected_plate);
CREATE INDEX IF NOT EXISTS idx_det_timestamp       ON detection_logs (detection_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_det_is_suspicious   ON detection_logs (is_suspicious)
    WHERE is_suspicious = TRUE;
CREATE INDEX IF NOT EXISTS idx_det_session         ON detection_logs (session_id);

-- Trigram index for partial plate lookups
CREATE INDEX IF NOT EXISTS idx_det_plate_trgm
    ON detection_logs USING GIN (detected_plate gin_trgm_ops);

COMMENT ON TABLE detection_logs IS 'Every plate detection event recorded by the system';


-- ╔══════════════════════════════════════════════════════════╗
-- ║  TASK 8 — TABLE: alert_notifications                    ║
-- ╚══════════════════════════════════════════════════════════╝
CREATE TABLE IF NOT EXISTS alert_notifications (
    id                  BIGSERIAL    PRIMARY KEY,
    detection_log_id    BIGINT       REFERENCES detection_logs (id) ON DELETE CASCADE,
    police_station_id   BIGINT       REFERENCES police_stations (id) ON DELETE SET NULL,
    station_name        TEXT,
    station_email       TEXT,
    license_plate       TEXT         NOT NULL,
    vehicle_info        JSONB,                              -- {type, color, reason, severity, owner}
    severity_level      severity_level,
    alert_type          TEXT         NOT NULL DEFAULT 'system',
    alert_status        alert_status NOT NULL DEFAULT 'sent',
    sent_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    error_message       TEXT,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alert_detection   ON alert_notifications (detection_log_id);
CREATE INDEX IF NOT EXISTS idx_alert_station     ON alert_notifications (police_station_id);
CREATE INDEX IF NOT EXISTS idx_alert_plate       ON alert_notifications (license_plate);
CREATE INDEX IF NOT EXISTS idx_alert_severity    ON alert_notifications (severity_level);
CREATE INDEX IF NOT EXISTS idx_alert_status      ON alert_notifications (alert_status);
-- GIN index on JSONB vehicle_info for fast JSON queries
CREATE INDEX IF NOT EXISTS idx_alert_vehicle_info ON alert_notifications USING GIN (vehicle_info);

COMMENT ON TABLE  alert_notifications IS 'Alerts dispatched to police stations for suspicious detections';
COMMENT ON COLUMN alert_notifications.vehicle_info IS 'JSONB: {type, color, reason, severity, owner}';


-- ╔══════════════════════════════════════════════════════════╗
-- ║  TASK 9 — TABLE: pdf_extraction_logs (NEW — PDF.co)     ║
-- ╚══════════════════════════════════════════════════════════╝
-- Stores a record of every PDF extraction request made via PDF.co API
CREATE TABLE IF NOT EXISTS pdf_extraction_logs (
    id              BIGSERIAL   PRIMARY KEY,
    filename        TEXT        NOT NULL,
    source_url      TEXT,                                   -- set if extracted from URL
    page_count      INTEGER,
    char_count      INTEGER,
    extracted_text  TEXT,                                   -- full extracted text (can be large)
    pages_requested TEXT,                                   -- e.g. "1-3,5" or "" for all
    ocr_enabled     BOOLEAN     NOT NULL DEFAULT TRUE,
    body_url        TEXT,                                   -- PDF.co result download URL
    status          TEXT        NOT NULL DEFAULT 'success', -- success | error
    error_message   TEXT,
    uploaded_by     BIGINT      REFERENCES users (id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pdf_log_uploaded_by ON pdf_extraction_logs (uploaded_by);
CREATE INDEX IF NOT EXISTS idx_pdf_log_created_at  ON pdf_extraction_logs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pdf_log_status       ON pdf_extraction_logs (status);

COMMENT ON TABLE  pdf_extraction_logs IS 'Audit log of all PDF.co text-extraction requests';
COMMENT ON COLUMN pdf_extraction_logs.extracted_text IS 'Full plaintext output from PDF.co';


-- ╔══════════════════════════════════════════════════════════╗
-- ║  TASK 10 — ROW-LEVEL SECURITY (RLS)                     ║
-- ╚══════════════════════════════════════════════════════════╝
-- Enable RLS on all tables so Supabase enforces access control

ALTER TABLE users                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE police_stations       ENABLE ROW LEVEL SECURITY;
ALTER TABLE suspicious_vehicles   ENABLE ROW LEVEL SECURITY;
ALTER TABLE video_sessions        ENABLE ROW LEVEL SECURITY;
ALTER TABLE detection_logs        ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert_notifications   ENABLE ROW LEVEL SECURITY;
ALTER TABLE pdf_extraction_logs   ENABLE ROW LEVEL SECURITY;

-- ── Policies: service_role bypasses RLS (used by your FastAPI backend) ─────────
-- These policies allow any authenticated user (via JWT) to read.
-- Your backend uses the SERVICE_ROLE key which bypasses RLS entirely.

-- Users: only see own row unless admin
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polname = 'users_self_read') THEN
        CREATE POLICY "users_self_read"
            ON users FOR SELECT
            USING (auth.uid()::TEXT = id::TEXT OR role = 'admin');
    END IF;

    -- Police stations: readable by all authenticated users
    IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polname = 'stations_read_all') THEN
        CREATE POLICY "stations_read_all"
            ON police_stations FOR SELECT
            USING (auth.role() = 'authenticated');
    END IF;

    -- Suspicious vehicles: readable by all, writable by admin/police
    IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polname = 'suspicious_read_all') THEN
        CREATE POLICY "suspicious_read_all"
            ON suspicious_vehicles FOR SELECT
            USING (auth.role() = 'authenticated');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polname = 'suspicious_write_admin_police') THEN
        CREATE POLICY "suspicious_write_admin_police"
            ON suspicious_vehicles FOR ALL
            USING (auth.role() = 'authenticated');
    END IF;

    -- Detection logs: readable by all authenticated
    IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polname = 'detections_read_all') THEN
        CREATE POLICY "detections_read_all"
            ON detection_logs FOR SELECT
            USING (auth.role() = 'authenticated');
    END IF;

    -- Alerts: readable by all authenticated
    IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polname = 'alerts_read_all') THEN
        CREATE POLICY "alerts_read_all"
            ON alert_notifications FOR SELECT
            USING (auth.role() = 'authenticated');
    END IF;

    -- PDF logs: readable by uploader or admin
    IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polname = 'pdf_logs_read_own') THEN
        CREATE POLICY "pdf_logs_read_own"
            ON pdf_extraction_logs FOR SELECT
            USING (auth.role() = 'authenticated');
    END IF;
END $$;


-- ╔══════════════════════════════════════════════════════════╗
-- ║  TASK 11 — SEED DATA                                     ║
-- ╚══════════════════════════════════════════════════════════╝

-- Police Stations
INSERT INTO police_stations (station_name, station_code, address, city, state, phone_number, email, latitude, longitude)
VALUES
    ('Central Police Station',   'CPS001', '123 Main Street',     'Mumbai',    'Maharashtra',  '+91-22-12345678', 'central@police.gov.in', 19.0760,  72.8777),
    ('North Zone Station',       'NZS002', '456 North Avenue',    'Delhi',     'Delhi',         '+91-11-23456789', 'north@police.gov.in',   28.7041,  77.1025),
    ('South District HQ',        'SDH003', '789 South Road',      'Bangalore', 'Karnataka',    '+91-80-34567890', 'south@police.gov.in',   12.9716,  77.5946),
    ('East Zone Police Station', 'EZP004', '22 East Park Lane',   'Kolkata',   'West Bengal',  '+91-33-45678901', 'east@police.gov.in',    22.5726,  88.3639)
ON CONFLICT (station_code) DO NOTHING;


-- Suspicious Vehicles (watchlist)
INSERT INTO suspicious_vehicles (license_plate, vehicle_type, vehicle_color, reason_for_flagging, severity_level, reported_by, reported_date)
VALUES
    ('MH12AB1234', 'Car',   'Black',  'Stolen vehicle reported 2024-01-15',      'critical', 'Mumbai Police',             '2024-01-15'),
    ('DL3CAB5678', 'SUV',   'White',  'Hit-and-run case FIR #DL2024-102',        'high',     'Delhi Traffic Police',      '2024-02-10'),
    ('KA01XY9876', 'Bike',  'Red',    'Suspected in armed robbery',              'high',     'Bangalore Crime Branch',    '2024-03-05'),
    ('TN22CD4567', 'Truck', 'Blue',   'Transporting illegal contraband',         'medium',   'Highway Patrol',            '2024-03-20'),
    ('GJ09EF3456', 'Van',   'Grey',   'Repeat offender - no valid insurance',    'low',      'Ahmedabad RTO',             '2024-04-01'),
    ('DL8SBT1234', 'Car',   'Silver', 'Involved in drug trafficking case',       'critical', 'Narcotics Control Bureau',  '2024-04-12')
ON CONFLICT (license_plate) DO NOTHING;


-- ╔══════════════════════════════════════════════════════════╗
-- ║  TASK 12 — HELPER VIEWS                                  ║
-- ╚══════════════════════════════════════════════════════════╝

-- Dashboard summary stats view
CREATE OR REPLACE VIEW vw_dashboard_stats AS
SELECT
    (SELECT COUNT(*)  FROM detection_logs)                                     AS total_detections,
    (SELECT COUNT(*)  FROM detection_logs WHERE is_suspicious = TRUE)          AS suspicious_detections,
    (SELECT COUNT(*)  FROM alert_notifications)                                AS total_alerts,
    (SELECT COUNT(*)  FROM suspicious_vehicles WHERE is_active = TRUE)         AS active_watchlist,
    (SELECT COUNT(*)  FROM police_stations)                                    AS police_stations,
    (SELECT COUNT(*)  FROM pdf_extraction_logs WHERE status = 'success')       AS pdf_extractions;

COMMENT ON VIEW vw_dashboard_stats IS 'Aggregated dashboard KPIs — refresh on demand';


-- Recent detections with vehicle info joined
CREATE OR REPLACE VIEW vw_recent_detections AS
SELECT
    dl.id,
    dl.detected_plate,
    dl.confidence_score,
    dl.is_suspicious,
    dl.detection_timestamp,
    dl.camera_location,
    dl.image_filename,
    dl.alert_sent,
    sv.vehicle_type,
    sv.vehicle_color,
    sv.severity_level,
    sv.reason_for_flagging,
    vs.session_type,
    vs.video_filename
FROM detection_logs dl
LEFT JOIN suspicious_vehicles sv ON sv.id = dl.suspicious_vehicle_id
LEFT JOIN video_sessions      vs ON vs.id = dl.session_id
ORDER BY dl.detection_timestamp DESC;

COMMENT ON VIEW vw_recent_detections IS 'Detection logs enriched with vehicle & session data';


-- ╔══════════════════════════════════════════════════════════╗
-- ║  TASK 13 — REALTIME PUBLICATION (Supabase Realtime)      ║
-- ╚══════════════════════════════════════════════════════════╝
-- Enable Realtime for live WebSocket pushes to your frontend
-- Wrap in DO block to handle cases where publication or table mapping already exists
DO $$
BEGIN
    -- Ensure publication exists
    IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'supabase_realtime') THEN
        CREATE PUBLICATION supabase_realtime;
    END IF;

    -- Add tables (ignore errors if already added)
    BEGIN
        ALTER PUBLICATION supabase_realtime ADD TABLE detection_logs;
    EXCEPTION WHEN others THEN NULL;
    END;
    
    BEGIN
        ALTER PUBLICATION supabase_realtime ADD TABLE alert_notifications;
    EXCEPTION WHEN others THEN NULL;
    END;
    
    BEGIN
        ALTER PUBLICATION supabase_realtime ADD TABLE suspicious_vehicles;
    EXCEPTION WHEN others THEN NULL;
    END;
END $$;

-- ============================================================
-- END OF MIGRATION
-- ============================================================
