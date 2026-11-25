-- IoT Device Bridge Enhancement Migration
-- Adds device schemas, command queue, and enhanced device columns
-- Version: 005
-- Date: 2026-01-13

-- ============================================================================
-- 1. IOT DEVICE SCHEMAS TABLE
-- Stores I/O definitions for each device (inputs = controls, outputs = readings)
-- ============================================================================

CREATE TABLE IF NOT EXISTS iot_device_schemas (
    id SERIAL PRIMARY KEY,
    device_id INTEGER NOT NULL,
    schema_version INTEGER DEFAULT 1,

    -- Input definitions (controls user can send to device)
    -- Example: [{"id": "relay", "type": "toggle", "label": "Main Relay", "default": false}]
    inputs JSONB DEFAULT '[]'::jsonb,

    -- Output definitions (readings device sends back)
    -- Example: [{"id": "temperature", "type": "numeric", "label": "Temp", "unit": "C", "widget": "gauge"}]
    outputs JSONB DEFAULT '[]'::jsonb,

    -- Dashboard layout configuration
    dashboard_config JSONB DEFAULT '{}'::jsonb,

    -- Widget styling overrides
    widget_styles JSONB DEFAULT '{}'::jsonb,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_schema_device FOREIGN KEY (device_id)
        REFERENCES iot_devices(id) ON DELETE CASCADE
);

-- Unique constraint: one schema per device
CREATE UNIQUE INDEX IF NOT EXISTS idx_iot_schemas_device ON iot_device_schemas(device_id);

-- ============================================================================
-- 2. IOT COMMAND QUEUE TABLE
-- Stores pending commands for devices (bidirectional communication)
-- ============================================================================

CREATE TABLE IF NOT EXISTS iot_command_queue (
    id SERIAL PRIMARY KEY,
    device_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,

    -- Command identification
    command_id VARCHAR(36) UNIQUE NOT NULL,  -- UUID for tracking
    command_type VARCHAR(50) NOT NULL DEFAULT 'set_input',  -- 'set_input', 'action', 'config'

    -- Command payload
    input_id VARCHAR(64),  -- Which input this targets (null for config commands)
    value JSONB NOT NULL,  -- The value to set

    -- Status tracking
    status VARCHAR(20) DEFAULT 'pending',  -- pending, sent, acknowledged, failed, expired
    priority INTEGER DEFAULT 0,  -- Higher = more urgent

    -- Timing
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMP,  -- When command was sent to device
    acknowledged_at TIMESTAMP,  -- When device confirmed receipt
    expires_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP + INTERVAL '5 minutes'),

    -- Response from device
    response JSONB,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,

    CONSTRAINT fk_cmd_device FOREIGN KEY (device_id)
        REFERENCES iot_devices(id) ON DELETE CASCADE,
    CONSTRAINT fk_cmd_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
);

-- Index for efficient command polling by device
CREATE INDEX IF NOT EXISTS idx_cmd_queue_device_status
    ON iot_command_queue(device_id, status);

-- Index for expired command cleanup
CREATE INDEX IF NOT EXISTS idx_cmd_queue_expires
    ON iot_command_queue(expires_at)
    WHERE status = 'pending';

-- Index for user command history
CREATE INDEX IF NOT EXISTS idx_cmd_queue_user
    ON iot_command_queue(user_id, created_at DESC);

-- ============================================================================
-- 3. ENHANCE IOT_DEVICES TABLE
-- Add columns for communication mode and current state
-- ============================================================================

-- Communication mode: polling (device polls server) or websocket (server pushes)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'iot_devices' AND column_name = 'communication_mode'
    ) THEN
        ALTER TABLE iot_devices ADD COLUMN communication_mode VARCHAR(20) DEFAULT 'polling';
    END IF;
END $$;

-- Track WebSocket connection status
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'iot_devices' AND column_name = 'websocket_connected'
    ) THEN
        ALTER TABLE iot_devices ADD COLUMN websocket_connected BOOLEAN DEFAULT false;
    END IF;
END $$;

-- Store current device state (latest input/output values)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'iot_devices' AND column_name = 'current_state'
    ) THEN
        ALTER TABLE iot_devices ADD COLUMN current_state JSONB DEFAULT '{}'::jsonb;
    END IF;
END $$;

-- Last command timestamp
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'iot_devices' AND column_name = 'last_command_at'
    ) THEN
        ALTER TABLE iot_devices ADD COLUMN last_command_at TIMESTAMP;
    END IF;
END $$;

-- SDK type used by device
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'iot_devices' AND column_name = 'sdk_type'
    ) THEN
        ALTER TABLE iot_devices ADD COLUMN sdk_type VARCHAR(20);
    END IF;
END $$;

-- Firmware version reported by device
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'iot_devices' AND column_name = 'firmware_version'
    ) THEN
        ALTER TABLE iot_devices ADD COLUMN firmware_version VARCHAR(50);
    END IF;
END $$;

-- ============================================================================
-- 4. HELPER FUNCTIONS
-- ============================================================================

-- Function to expire old pending commands
CREATE OR REPLACE FUNCTION expire_old_iot_commands() RETURNS INTEGER AS $$
DECLARE
    expired_count INTEGER;
BEGIN
    UPDATE iot_command_queue
    SET status = 'expired'
    WHERE status = 'pending'
      AND expires_at < CURRENT_TIMESTAMP;

    GET DIAGNOSTICS expired_count = ROW_COUNT;
    RETURN expired_count;
END;
$$ LANGUAGE plpgsql;

-- Function to update device state when readings come in
CREATE OR REPLACE FUNCTION update_device_state(
    p_device_id INTEGER,
    p_readings JSONB
) RETURNS VOID AS $$
BEGIN
    UPDATE iot_devices
    SET current_state = COALESCE(current_state, '{}'::jsonb) || p_readings,
        last_seen = CURRENT_TIMESTAMP,
        status = 'online'
    WHERE id = p_device_id;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- 5. COMMENTS FOR DOCUMENTATION
-- ============================================================================

COMMENT ON TABLE iot_device_schemas IS 'Device I/O schema definitions for auto-generated dashboards';
COMMENT ON TABLE iot_command_queue IS 'Command queue for bidirectional device communication';

COMMENT ON COLUMN iot_device_schemas.inputs IS 'Array of input definitions: [{id, type, label, min, max, default, ...}]';
COMMENT ON COLUMN iot_device_schemas.outputs IS 'Array of output definitions: [{id, type, label, unit, widget, ...}]';

COMMENT ON COLUMN iot_command_queue.command_type IS 'Type: set_input (change value), action (trigger), config (update settings)';
COMMENT ON COLUMN iot_command_queue.status IS 'Status: pending, sent, acknowledged, failed, expired';

COMMENT ON COLUMN iot_devices.communication_mode IS 'Device communication: polling (device polls) or websocket (server pushes)';
COMMENT ON COLUMN iot_devices.current_state IS 'Latest known state of all inputs and outputs as JSONB';

-- ============================================================================
-- Migration complete
-- ============================================================================
