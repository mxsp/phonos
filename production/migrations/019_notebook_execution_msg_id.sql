-- Description: Add msg_id column to notebook_executions for exact execution matching.
-- This fixes the bug where overlapping cell executions could update the wrong row,
-- and allows the backend to reliably mark the correct execution as finished even
-- when cells produce no output.

-- Add msg_id column
ALTER TABLE notebook_executions
    ADD COLUMN IF NOT EXISTS msg_id VARCHAR(64);

-- Index for fast lookup by kernel message id
CREATE INDEX IF NOT EXISTS idx_notebook_executions_msg_id ON notebook_executions(msg_id);

ANALYZE notebook_executions;
