-- Migration: Add api_webhooks table for webhook management
-- This table stores user-configured webhooks for job notifications

CREATE TABLE IF NOT EXISTS api_webhooks (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    events TEXT NOT NULL,  -- JSON array of event types
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_triggered_at TIMESTAMP,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT check_https CHECK (url LIKE 'https://%')
);

-- Add index for faster user queries
CREATE INDEX IF NOT EXISTS idx_api_webhooks_user_id ON api_webhooks(user_id);
CREATE INDEX IF NOT EXISTS idx_api_webhooks_active ON api_webhooks(user_id, is_active);

-- Add comment for documentation
COMMENT ON TABLE api_webhooks IS 'User-configured webhooks for receiving job status notifications';
COMMENT ON COLUMN api_webhooks.events IS 'JSON array of event types (job.completed, job.failed, job.started)';
COMMENT ON COLUMN api_webhooks.is_active IS '1 for active webhooks, 0 for disabled';
