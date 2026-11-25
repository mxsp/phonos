-- IoT Bridge V2 - Complete Rework
-- Device-centric authentication with API keys and encryption
-- Version: 010
-- Date: 2026-01-18

-- ============================================================================
-- 1. IOT DEVICES TABLE
-- Core device registration with unique API keys
-- ============================================================================

CREATE TABLE IF NOT EXISTS iot_devices (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,

    -- Device identification
    device_id VARCHAR(64) NOT NULL,           -- User-defined unique ID (e.g., "lab-sensor-01")
    device_name VARCHAR(128) NOT NULL,        -- Human-readable name
    device_type VARCHAR(64) DEFAULT 'generic', -- Device category
    description TEXT,

    -- Authentication & Encryption
    api_key VARCHAR(64) NOT NULL UNIQUE,      -- Device API key (also used for encryption)
    api_key_hash VARCHAR(128) NOT NULL,       -- SHA-256 hash for fast lookup
    encryption_enabled BOOLEAN DEFAULT true,  -- Whether to encrypt data

    -- Device Schema (inputs/outputs definition)
    schema JSONB DEFAULT '{
        "inputs": [],
        "outputs": [],
        "config": {}
    }'::jsonb,

    -- Current state & status
    current_state JSONB DEFAULT '{}'::jsonb,  -- Latest values
    status VARCHAR(20) DEFAULT 'offline',     -- online, offline, error
    last_seen TIMESTAMP,
    last_data_at TIMESTAMP,

    -- Metadata
    config JSONB DEFAULT '{}'::jsonb,         -- Device configuration
    tags TEXT[],                              -- Searchable tags
    project_id INTEGER,                       -- Optional project association

    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Constraints
    CONSTRAINT fk_iot_device_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_iot_device_project FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE SET NULL,
    CONSTRAINT uq_iot_device_user_device_id UNIQUE (user_id, device_id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_iot_devices_user ON iot_devices(user_id);
CREATE INDEX IF NOT EXISTS idx_iot_devices_api_key_hash ON iot_devices(api_key_hash);
CREATE INDEX IF NOT EXISTS idx_iot_devices_status ON iot_devices(status);
CREATE INDEX IF NOT EXISTS idx_iot_devices_project ON iot_devices(project_id);

-- ============================================================================
-- 2. IOT DEVICE DATA TABLE
-- Time-series data storage for device readings
-- ============================================================================

CREATE TABLE IF NOT EXISTS iot_device_data (
    id BIGSERIAL PRIMARY KEY,
    device_id INTEGER NOT NULL,

    -- Data payload
    data JSONB NOT NULL,                      -- The actual data (may be encrypted)
    encrypted BOOLEAN DEFAULT false,          -- Whether data is encrypted

    -- Metadata
    data_type VARCHAR(32) DEFAULT 'reading',  -- reading, event, status, error
    source VARCHAR(64),                       -- Which sensor/component

    -- Timestamps
    device_timestamp TIMESTAMP,               -- Timestamp from device
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_iot_data_device FOREIGN KEY (device_id)
        REFERENCES iot_devices(id) ON DELETE CASCADE
);

-- Indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_iot_data_device_time
    ON iot_device_data(device_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_iot_data_type
    ON iot_device_data(device_id, data_type);

-- ============================================================================
-- 3. IOT COMMANDS TABLE
-- Commands sent from web clients to devices
-- ============================================================================

CREATE TABLE IF NOT EXISTS iot_commands (
    id SERIAL PRIMARY KEY,
    device_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,

    -- Command identification
    command_id VARCHAR(36) NOT NULL UNIQUE,   -- UUID
    command_type VARCHAR(32) NOT NULL,        -- set_value, action, config

    -- Command payload
    target VARCHAR(64),                       -- Which input/control to target
    payload JSONB NOT NULL,                   -- Command data
    encrypted BOOLEAN DEFAULT false,

    -- Status tracking
    status VARCHAR(20) DEFAULT 'pending',     -- pending, delivered, acknowledged, failed, expired
    priority INTEGER DEFAULT 0,

    -- Timing
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    delivered_at TIMESTAMP,
    acknowledged_at TIMESTAMP,
    expires_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP + INTERVAL '5 minutes'),

    -- Response
    response JSONB,
    error_message TEXT,

    CONSTRAINT fk_iot_cmd_device FOREIGN KEY (device_id)
        REFERENCES iot_devices(id) ON DELETE CASCADE,
    CONSTRAINT fk_iot_cmd_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_iot_cmd_device_status
    ON iot_commands(device_id, status);
CREATE INDEX IF NOT EXISTS idx_iot_cmd_pending
    ON iot_commands(device_id, created_at)
    WHERE status = 'pending';

-- ============================================================================
-- 4. IOT ACCESS TOKENS TABLE
-- For web client access to specific devices
-- ============================================================================

CREATE TABLE IF NOT EXISTS iot_access_tokens (
    id SERIAL PRIMARY KEY,
    device_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,

    -- Token
    token VARCHAR(64) NOT NULL UNIQUE,
    token_hash VARCHAR(128) NOT NULL,

    -- Permissions
    can_read BOOLEAN DEFAULT true,
    can_write BOOLEAN DEFAULT false,
    can_admin BOOLEAN DEFAULT false,

    -- Validity
    expires_at TIMESTAMP,
    last_used_at TIMESTAMP,

    -- Metadata
    name VARCHAR(128),                        -- Token name/description
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_iot_token_device FOREIGN KEY (device_id)
        REFERENCES iot_devices(id) ON DELETE CASCADE,
    CONSTRAINT fk_iot_token_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_iot_tokens_hash ON iot_access_tokens(token_hash);

-- ============================================================================
-- 5. HELPER FUNCTIONS
-- ============================================================================

-- Update device last_seen on activity
CREATE OR REPLACE FUNCTION update_iot_device_activity(p_device_id INTEGER)
RETURNS VOID AS $$
BEGIN
    UPDATE iot_devices
    SET last_seen = CURRENT_TIMESTAMP,
        status = 'online',
        updated_at = CURRENT_TIMESTAMP
    WHERE id = p_device_id;
END;
$$ LANGUAGE plpgsql;

-- Expire old commands
CREATE OR REPLACE FUNCTION expire_iot_commands()
RETURNS INTEGER AS $$
DECLARE
    count INTEGER;
BEGIN
    UPDATE iot_commands
    SET status = 'expired'
    WHERE status = 'pending' AND expires_at < CURRENT_TIMESTAMP;
    GET DIAGNOSTICS count = ROW_COUNT;
    RETURN count;
END;
$$ LANGUAGE plpgsql;

-- Mark devices offline after inactivity
CREATE OR REPLACE FUNCTION mark_iot_devices_offline(p_timeout_minutes INTEGER DEFAULT 5)
RETURNS INTEGER AS $$
DECLARE
    count INTEGER;
BEGIN
    UPDATE iot_devices
    SET status = 'offline'
    WHERE status = 'online'
      AND last_seen < (CURRENT_TIMESTAMP - (p_timeout_minutes || ' minutes')::INTERVAL);
    GET DIAGNOSTICS count = ROW_COUNT;
    RETURN count;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- Migration complete
-- ============================================================================
