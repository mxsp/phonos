-- ================================================================
-- PHONOS NOTEBOOK SYSTEM - Database Migration
-- Migration: 016_add_runtime_level_to_sessions.sql
-- Description: Add runtime_level column to notebook_sessions table
-- ================================================================

-- Add runtime_level column to notebook_sessions
ALTER TABLE notebook_sessions
ADD COLUMN IF NOT EXISTS runtime_level VARCHAR(50) DEFAULT 'basic';

-- Create index for runtime_level queries
CREATE INDEX IF NOT EXISTS idx_notebook_sessions_runtime_level ON notebook_sessions(runtime_level);

-- Update the view to include runtime_level
CREATE OR REPLACE VIEW active_notebook_sessions AS
SELECT
    ns.*,
    n.name as notebook_name,
    n.project_id,
    u.username,
    p.name as project_name
FROM notebook_sessions ns
JOIN notebooks n ON ns.notebook_id = n.id
JOIN users u ON ns.user_id = u.id
LEFT JOIN projects p ON n.project_id = p.id
WHERE ns.kernel_status IN ('starting', 'idle', 'busy')
AND ns.ended_at IS NULL;

-- Analyze the table
ANALYZE notebook_sessions;
