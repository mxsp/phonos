-- Migration: IoT Dashboard Layouts
-- Creates tables for storing device dashboard configurations

-- Dashboard layout storage
CREATE TABLE IF NOT EXISTS iot_dashboards (
    id SERIAL PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES iot_devices(id) ON DELETE CASCADE,
    name VARCHAR(128) NOT NULL DEFAULT 'Default Dashboard',
    layout JSONB NOT NULL DEFAULT '{"widgets": [], "grid": {"columns": 4, "rowHeight": 100, "gap": 8}}',
    is_default BOOLEAN DEFAULT false,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(device_id, name)
);

-- Index for fast lookups
CREATE INDEX IF NOT EXISTS idx_iot_dashboards_device ON iot_dashboards(device_id);
CREATE INDEX IF NOT EXISTS idx_iot_dashboards_default ON iot_dashboards(device_id, is_default) WHERE is_default = true;

-- Add dashboard_enabled flag to devices if not exists
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'iot_devices' AND column_name = 'dashboard_enabled'
    ) THEN
        ALTER TABLE iot_devices ADD COLUMN dashboard_enabled BOOLEAN DEFAULT true;
    END IF;
END $$;

-- Create trigger function if not exists
CREATE OR REPLACE FUNCTION update_iot_dashboard_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create trigger
DROP TRIGGER IF EXISTS update_iot_dashboards_timestamp ON iot_dashboards;
CREATE TRIGGER update_iot_dashboards_timestamp
    BEFORE UPDATE ON iot_dashboards
    FOR EACH ROW EXECUTE FUNCTION update_iot_dashboard_timestamp();

-- Ensure only one default dashboard per device
CREATE OR REPLACE FUNCTION ensure_single_default_dashboard()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.is_default = true THEN
        UPDATE iot_dashboards
        SET is_default = false
        WHERE device_id = NEW.device_id AND id != NEW.id AND is_default = true;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ensure_single_default_dashboard_trigger ON iot_dashboards;
CREATE TRIGGER ensure_single_default_dashboard_trigger
    BEFORE INSERT OR UPDATE ON iot_dashboards
    FOR EACH ROW EXECUTE FUNCTION ensure_single_default_dashboard();

-- Widget presets table for reusable widget configurations
CREATE TABLE IF NOT EXISTS iot_widget_presets (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(128) NOT NULL,
    widget_type VARCHAR(32) NOT NULL,
    config JSONB NOT NULL DEFAULT '{}',
    is_global BOOLEAN DEFAULT false,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_iot_widget_presets_user ON iot_widget_presets(user_id);
CREATE INDEX IF NOT EXISTS idx_iot_widget_presets_type ON iot_widget_presets(widget_type);

-- Comment on tables
COMMENT ON TABLE iot_dashboards IS 'Stores visual dashboard layouts for IoT devices';
COMMENT ON TABLE iot_widget_presets IS 'Reusable widget configuration presets';

-- Layout JSON schema documentation
COMMENT ON COLUMN iot_dashboards.layout IS 'JSON structure: {"widgets": [{"id", "type", "x", "y", "w", "h", "config"}], "grid": {"columns", "rowHeight", "gap"}}';
