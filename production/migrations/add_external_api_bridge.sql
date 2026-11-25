-- Migration: Add External API Bridge Support
-- Date: 2026-01-03
-- Description: Adds support for worker-based job processing via external API bridge

-- Add new columns to api_keys table for worker keys
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS key_prefix VARCHAR(20);
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS key_type VARCHAR(20) DEFAULT 'public';
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS scope_model_type VARCHAR(50);
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS permissions TEXT;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS rate_limit_per_minute INTEGER DEFAULT 60;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS total_requests INTEGER DEFAULT 0;

-- Create index for key_type filtering
CREATE INDEX IF NOT EXISTS idx_api_keys_key_type ON api_keys(key_type);
CREATE INDEX IF NOT EXISTS idx_api_keys_user_type ON api_keys(user_id, key_type);

-- Create external API job queue table for standard models
CREATE TABLE IF NOT EXISTS external_api_job_queue (
    id SERIAL PRIMARY KEY,

    -- Job reference
    job_id INTEGER NOT NULL,
    job_uuid VARCHAR(100) NOT NULL UNIQUE,

    -- User who submitted the job
    user_id INTEGER NOT NULL,

    -- Model type (type-0, type-2, type-3, etc.)
    model_type VARCHAR(50) NOT NULL,

    -- Job data (JSON)
    job_data TEXT,

    -- Processing configuration
    timeout_seconds INTEGER DEFAULT 3600,
    priority INTEGER DEFAULT 0,

    -- Status tracking
    status VARCHAR(20) DEFAULT 'pending',  -- pending, assigned, processing, completed, failed
    progress INTEGER DEFAULT 0,
    progress_message TEXT,

    -- Worker assignment
    worker_api_key_id INTEGER,
    worker_identifier VARCHAR(255),

    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    assigned_at TIMESTAMP,
    last_heartbeat TIMESTAMP,
    completed_at TIMESTAMP,

    -- Foreign keys
    CONSTRAINT fk_ext_job_queue_job FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE,
    CONSTRAINT fk_ext_job_queue_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_ext_job_queue_worker_key FOREIGN KEY (worker_api_key_id) REFERENCES api_keys(id) ON DELETE SET NULL
);

-- Indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_ext_job_queue_user_status ON external_api_job_queue(user_id, status);
CREATE INDEX IF NOT EXISTS idx_ext_job_queue_model_status ON external_api_job_queue(model_type, status);
CREATE INDEX IF NOT EXISTS idx_ext_job_queue_job_uuid ON external_api_job_queue(job_uuid);
CREATE INDEX IF NOT EXISTS idx_ext_job_queue_worker ON external_api_job_queue(worker_api_key_id);
CREATE INDEX IF NOT EXISTS idx_ext_job_queue_created ON external_api_job_queue(created_at);

-- Add comment for documentation
COMMENT ON TABLE external_api_job_queue IS 'Queue for jobs to be processed by external workers via the bridge API';
COMMENT ON COLUMN external_api_job_queue.status IS 'Job status: pending (awaiting worker), assigned (claimed by worker), completed, failed';
COMMENT ON COLUMN external_api_job_queue.last_heartbeat IS 'Last heartbeat from worker, used to detect stale jobs';
