-- ================================================================
-- PHONOS TRUSTED TESTER PROGRAM - Database Migration
-- Migration: 023_trusted_tester_program.sql
-- Description: Access tiers (applicant -> tester -> regular) and the
--              Trusted Tester application/review table.
--
-- NOT auto-run: the same schema is folded into production/database.py
-- (_create_schema) and self-applies on boot. Kept here for review and
-- for applying by hand to a database that is not booted through init_db().
-- ================================================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS access_tier TEXT NOT NULL DEFAULT 'regular';

CREATE TABLE IF NOT EXISTS trusted_tester_applications (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    status          TEXT    NOT NULL DEFAULT 'pending',   -- pending|approved|rejected|graduated|revoked
    motivation      TEXT,
    institution     TEXT,
    use_case        TEXT,
    applied_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reviewed_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reviewed_at     TIMESTAMP,
    review_note     TEXT,
    graduated_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trusted_tester_status ON trusted_tester_applications(status);
