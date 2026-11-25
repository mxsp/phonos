-- ================================================================
-- PHONOS NOTEBOOK IMPROVEMENTS - Database Migration
-- Migration: 015_notebook_improvements.sql
-- Description: Add user preferences, runtime levels, and enhance
--              notebook sessions/executions for the improved IDE
-- ================================================================

-- ================================================================
-- NEW TABLES
-- ================================================================

-- User preferences (notebook settings, UI preferences, etc.)
CREATE TABLE IF NOT EXISTS user_preferences (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    preferences JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_user_preferences_user_id UNIQUE (user_id)
);

-- Runtime levels (machine tiers for notebook kernels)
CREATE TABLE IF NOT EXISTS runtime_levels (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL UNIQUE,
    display_name VARCHAR(100) NOT NULL,
    backend_type VARCHAR(30) NOT NULL,
    config_encrypted TEXT,
    mem_limit VARCHAR(20) DEFAULT '2g',
    cpu_quota REAL DEFAULT 1.0,
    gpu_enabled BOOLEAN DEFAULT FALSE,
    max_sessions INTEGER DEFAULT 5,
    sort_order INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT check_backend_type CHECK (
        backend_type IN ('local_docker', 'remote_docker', 'ssh', 'kubernetes')
    )
);

-- ================================================================
-- ALTER EXISTING TABLES
-- ================================================================

-- Add runtime_level and resource_config to notebook_sessions
ALTER TABLE notebook_sessions
    ADD COLUMN IF NOT EXISTS runtime_level VARCHAR(50) DEFAULT 'basic',
    ADD COLUMN IF NOT EXISTS resource_config JSONB DEFAULT '{}';

-- Add duration_ms, tags, and annotation to notebook_executions
ALTER TABLE notebook_executions
    ADD COLUMN IF NOT EXISTS duration_ms INTEGER,
    ADD COLUMN IF NOT EXISTS tags JSONB DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS annotation TEXT;

-- Add settings to notebooks
ALTER TABLE notebooks
    ADD COLUMN IF NOT EXISTS settings JSONB DEFAULT '{}';

-- ================================================================
-- INDEXES
-- ================================================================

CREATE INDEX IF NOT EXISTS idx_user_preferences_user_id ON user_preferences(user_id);
CREATE INDEX IF NOT EXISTS idx_runtime_levels_name ON runtime_levels(name);
CREATE INDEX IF NOT EXISTS idx_runtime_levels_active ON runtime_levels(is_active, sort_order)
    WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_notebook_executions_tags ON notebook_executions USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_notebook_sessions_runtime ON notebook_sessions(runtime_level);

-- ================================================================
-- TRIGGERS
-- ================================================================

-- Auto-update updated_at for user_preferences
CREATE OR REPLACE FUNCTION update_user_preferences_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS user_preferences_updated_at_trigger ON user_preferences;
CREATE TRIGGER user_preferences_updated_at_trigger
    BEFORE UPDATE ON user_preferences
    FOR EACH ROW
    EXECUTE FUNCTION update_user_preferences_timestamp();

-- Auto-update updated_at for runtime_levels
CREATE OR REPLACE FUNCTION update_runtime_levels_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS runtime_levels_updated_at_trigger ON runtime_levels;
CREATE TRIGGER runtime_levels_updated_at_trigger
    BEFORE UPDATE ON runtime_levels
    FOR EACH ROW
    EXECUTE FUNCTION update_runtime_levels_timestamp();

-- ================================================================
-- SEED DATA
-- ================================================================

-- Default 'basic' runtime level (local Docker, 2GB RAM, 1 CPU)
INSERT INTO runtime_levels (name, display_name, backend_type, mem_limit, cpu_quota, gpu_enabled, max_sessions, sort_order, is_active)
VALUES ('basic', 'Basic', 'local_docker', '2g', 1.0, FALSE, 5, 0, TRUE)
ON CONFLICT (name) DO NOTHING;

-- Seed a 'wolfram' runtime level so admins can configure the Wolfram service
-- URL via the /api/notebooks/runtimes API instead of relying solely on the
-- Python-side synthesized default. backend_type 'wolfram_service' is valid
-- (see runtime_config.VALID_BACKEND_TYPES) but was missing from the original
-- CHECK constraint, so a fresh PostgreSQL install rejected this INSERT.
INSERT INTO runtime_levels (name, display_name, backend_type, mem_limit, cpu_quota, gpu_enabled, max_sessions, sort_order, is_active)
VALUES ('wolfram', 'Wolfram Language', 'wolfram_service', '1g', 0.5, FALSE, 3, 10, TRUE)
ON CONFLICT (name) DO NOTHING;

-- ================================================================
-- RELAX backend_type CHECK CONSTRAINT
-- ================================================================
-- The original check_backend_type only allowed local_docker / remote_docker /
-- ssh / kubernetes. runtime_config.VALID_BACKEND_TYPES also allows
-- 'remote_ssh' and 'wolfram_service'. Drop & recreate the constraint so a
-- fresh PostgreSQL install accepts the wolfram_service backend type used by
-- the Wolfram evaluation service.
ALTER TABLE runtime_levels DROP CONSTRAINT IF EXISTS check_backend_type;
ALTER TABLE runtime_levels
    ADD CONSTRAINT check_backend_type CHECK (
        backend_type IN ('local_docker', 'remote_docker', 'ssh', 'kubernetes',
                         'remote_ssh', 'wolfram_service')
    );

-- ================================================================
-- ANALYZE
-- ================================================================

ANALYZE user_preferences;
ANALYZE runtime_levels;
ANALYZE notebook_sessions;
ANALYZE notebook_executions;
ANALYZE notebooks;
