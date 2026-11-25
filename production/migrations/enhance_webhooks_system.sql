-- Migration: Enhance webhooks system with delivery tracking and security

-- Enable pgcrypto extension for random bytes
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Add webhook delivery logs table
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id SERIAL PRIMARY KEY,
    webhook_id INTEGER NOT NULL REFERENCES api_webhooks(id) ON DELETE CASCADE,
    event_type VARCHAR(50) NOT NULL,
    payload TEXT NOT NULL,
    response_status INTEGER,
    response_body TEXT,
    response_time_ms INTEGER,
    error_message TEXT,
    delivered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    retry_count INTEGER DEFAULT 0
);

-- Add indexes for webhook deliveries
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_webhook_id ON webhook_deliveries(webhook_id);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_delivered_at ON webhook_deliveries(delivered_at);

-- Add description field to webhooks
ALTER TABLE api_webhooks
ADD COLUMN IF NOT EXISTS description TEXT,
ADD COLUMN IF NOT EXISTS retry_enabled INTEGER DEFAULT 1,
ADD COLUMN IF NOT EXISTS max_retries INTEGER DEFAULT 3,
ADD COLUMN IF NOT EXISTS timeout_seconds INTEGER DEFAULT 10;

-- Update secret_key to be NOT NULL with default if missing
UPDATE api_webhooks SET secret_key = encode(gen_random_bytes(32), 'hex') WHERE secret_key IS NULL OR secret_key = '';

COMMENT ON TABLE webhook_deliveries IS 'Tracks all webhook delivery attempts with responses and errors';
COMMENT ON COLUMN api_webhooks.description IS 'User-provided description of webhook purpose';
COMMENT ON COLUMN api_webhooks.retry_enabled IS 'Whether to retry failed deliveries (1=yes, 0=no)';
COMMENT ON COLUMN api_webhooks.max_retries IS 'Maximum number of retry attempts for failed deliveries';
COMMENT ON COLUMN api_webhooks.timeout_seconds IS 'Request timeout in seconds (default 10)';
