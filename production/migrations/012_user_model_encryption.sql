-- Migration: Add encryption support for User Model API keys
-- Date: 2026-01-20
-- Description: Adds encryption_enabled flag and updates schema for encrypted communications

-- Add encryption_enabled column to user_model_api_keys
ALTER TABLE user_model_api_keys
ADD COLUMN IF NOT EXISTS encryption_enabled BOOLEAN DEFAULT true;

-- Add index for faster lookups on encryption-enabled keys
CREATE INDEX IF NOT EXISTS idx_user_model_api_keys_encryption
ON user_model_api_keys(is_active, encryption_enabled)
WHERE is_active = true;

-- Add comment for documentation
COMMENT ON COLUMN user_model_api_keys.encryption_enabled IS
'Whether encrypted communication is enabled for this API key. When true, workers should use AES-256-GCM encryption for job data.';

-- Ensure encrypted_key column exists (for storing encrypted API keys)
ALTER TABLE user_model_api_keys
ALTER COLUMN encrypted_key TYPE TEXT;

-- Add encryption metadata column for tracking encryption version/algorithm
ALTER TABLE user_model_api_keys
ADD COLUMN IF NOT EXISTS encryption_version INTEGER DEFAULT 1;

COMMENT ON COLUMN user_model_api_keys.encryption_version IS
'Encryption version used: 1=AES-256-GCM, 0=XOR fallback (legacy)';

-- Update existing keys to have encryption enabled by default
UPDATE user_model_api_keys
SET encryption_enabled = true
WHERE encryption_enabled IS NULL;

-- Add signature tracking for request verification
ALTER TABLE user_model_api_keys
ADD COLUMN IF NOT EXISTS require_signature BOOLEAN DEFAULT false;

COMMENT ON COLUMN user_model_api_keys.require_signature IS
'Whether HMAC-SHA256 request signatures are required for this API key.';

-- Create table for tracking encrypted communications (audit log)
CREATE TABLE IF NOT EXISTS user_model_encryption_log (
    id SERIAL PRIMARY KEY,
    api_key_id INTEGER REFERENCES user_model_api_keys(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    operation VARCHAR(50) NOT NULL, -- 'encrypt', 'decrypt', 'sign', 'verify'
    success BOOLEAN NOT NULL DEFAULT true,
    error_message TEXT,
    encryption_version INTEGER DEFAULT 1,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_encryption_log_key_id ON user_model_encryption_log(api_key_id);
CREATE INDEX IF NOT EXISTS idx_encryption_log_timestamp ON user_model_encryption_log(timestamp);

-- Add encryption_enabled to external_job_queue for tracking
ALTER TABLE external_job_queue
ADD COLUMN IF NOT EXISTS encrypted BOOLEAN DEFAULT false;

COMMENT ON COLUMN external_job_queue.encrypted IS
'Whether the job data in this queue entry is encrypted.';

-- Add encryption status to jobs table
ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS job_data_encrypted BOOLEAN DEFAULT false;

COMMENT ON COLUMN jobs.job_data_encrypted IS
'Whether the job_data field contains encrypted content.';

-- Migration complete
SELECT 'Migration 012_user_model_encryption completed successfully' AS status;
