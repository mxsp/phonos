-- Migration: Add source tracking for notebook-sourced jobs
-- Date: 2026-01-22
-- Description: Adds source column to track where jobs originate from (notebook, api, web, etc.)

-- Add source column to jobs table if it doesn't exist
ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS source VARCHAR(50) DEFAULT 'web';

COMMENT ON COLUMN jobs.source IS
'Job source: notebook, api, web, cli. Used to identify how the job was submitted.';

-- Add index for filtering by source
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);

-- Update existing jobs to have 'web' source (default)
UPDATE jobs
SET source = 'web'
WHERE source IS NULL;

-- Migration complete
SELECT 'Migration 013_notebook_job_source completed successfully' AS status;
