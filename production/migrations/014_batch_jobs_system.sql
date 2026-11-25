-- Migration: Add Batch Jobs system
-- Date: 2026-01-23
-- Description: Creates batches table to implement Project-Batch-Jobs-JobDetail hierarchy

-- Create batches table
CREATE TABLE IF NOT EXISTS batches (
    id SERIAL PRIMARY KEY,
    batch_id VARCHAR(50) UNIQUE NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    creator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE SET NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    status VARCHAR(50) DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'mixed')),
    total_jobs INTEGER DEFAULT 0,
    completed_jobs INTEGER DEFAULT 0,
    failed_jobs INTEGER DEFAULT 0,
    cancelled_jobs INTEGER DEFAULT 0,
    total_cost DECIMAL(10,6) DEFAULT 0.000000,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Add indexes for performance
CREATE INDEX IF NOT EXISTS idx_batches_project_id ON batches(project_id);
CREATE INDEX IF NOT EXISTS idx_batches_creator_id ON batches(creator_id);
CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status);
CREATE INDEX IF NOT EXISTS idx_batches_created_at ON batches(created_at);

-- Add batch_id column to jobs table to link jobs to batches
ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS batch_id INTEGER REFERENCES batches(id) ON DELETE SET NULL;

-- Add index for batch_id in jobs table
CREATE INDEX IF NOT EXISTS idx_jobs_batch_id ON jobs(batch_id);

-- Create trigger to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_batches_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_update_batches_updated_at
    BEFORE UPDATE ON batches
    FOR EACH ROW
    EXECUTE FUNCTION update_batches_updated_at();

-- Create trigger to update batch job counts when job status changes
CREATE OR REPLACE FUNCTION update_batch_job_counts()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.batch_id IS NOT NULL THEN
        UPDATE batches SET
            total_jobs = (
                SELECT COUNT(*) FROM jobs WHERE batch_id = NEW.batch_id
            ),
            completed_jobs = (
                SELECT COUNT(*) FROM jobs WHERE batch_id = NEW.batch_id AND status = 'completed'
            ),
            failed_jobs = (
                SELECT COUNT(*) FROM jobs WHERE batch_id = NEW.batch_id AND status = 'failed'
            ),
            cancelled_jobs = (
                SELECT COUNT(*) FROM jobs WHERE batch_id = NEW.batch_id AND status = 'cancelled'
            ),
            total_cost = (
                SELECT COALESCE(SUM(cost), 0) FROM jobs WHERE batch_id = NEW.batch_id
            ),
            status = CASE
                WHEN (SELECT COUNT(*) FROM jobs WHERE batch_id = NEW.batch_id AND status IN ('pending', 'running')) > 0
                     AND (SELECT COUNT(*) FROM jobs WHERE batch_id = NEW.batch_id AND status IN ('completed', 'failed')) > 0
                THEN 'mixed'
                WHEN (SELECT COUNT(*) FROM jobs WHERE batch_id = NEW.batch_id AND status = 'pending') = 
                     (SELECT COUNT(*) FROM jobs WHERE batch_id = NEW.batch_id)
                THEN 'pending'
                WHEN (SELECT COUNT(*) FROM jobs WHERE batch_id = NEW.batch_id AND status = 'running') > 0
                THEN 'running'
                WHEN (SELECT COUNT(*) FROM jobs WHERE batch_id = NEW.batch_id AND status = 'completed') = 
                     (SELECT COUNT(*) FROM jobs WHERE batch_id = NEW.batch_id)
                THEN 'completed'
                WHEN (SELECT COUNT(*) FROM jobs WHERE batch_id = NEW.batch_id AND status = 'failed') = 
                     (SELECT COUNT(*) FROM jobs WHERE batch_id = NEW.batch_id)
                THEN 'failed'
                WHEN (SELECT COUNT(*) FROM jobs WHERE batch_id = NEW.batch_id AND status = 'cancelled') = 
                     (SELECT COUNT(*) FROM jobs WHERE batch_id = NEW.batch_id)
                THEN 'cancelled'
                ELSE 'mixed'
            END
        WHERE id = NEW.batch_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_update_batch_job_counts
    AFTER INSERT OR UPDATE ON jobs
    FOR EACH ROW
    EXECUTE FUNCTION update_batch_job_counts();

-- Add comments
COMMENT ON TABLE batches IS 'Batch job containers for organizing related jobs within projects';
COMMENT ON COLUMN batches.batch_id IS 'Human-readable batch identifier';
COMMENT ON COLUMN batches.status IS 'Batch status: pending, running, completed, failed, cancelled, mixed';
COMMENT ON COLUMN batches.total_jobs IS 'Total number of jobs in this batch';
COMMENT ON COLUMN batches.completed_jobs IS 'Number of completed jobs in this batch';
COMMENT ON COLUMN batches.failed_jobs IS 'Number of failed jobs in this batch';
COMMENT ON COLUMN batches.cancelled_jobs IS 'Number of cancelled jobs in this batch';
COMMENT ON COLUMN batches.total_cost IS 'Total cost of all jobs in this batch';
COMMENT ON COLUMN jobs.batch_id IS 'Optional link to a batch container for this job';

-- Migration complete
SELECT 'Migration 014_batch_jobs_system completed successfully' AS status;