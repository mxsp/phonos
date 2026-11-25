-- Migration 004: File Performance Indexes
-- Optimizes file and job queries with strategic indexes
-- Date: 2026-01-06

-- ============================================================================
-- FILE LISTING PERFORMANCE INDEXES
-- ============================================================================

-- Covering index for file listings (includes all columns needed for display)
-- This allows index-only scans for common file listing queries
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_files_project_details
    ON files(project_id, created_at DESC, id, name, size, notes);

-- Index for file creator queries (user's uploads across projects)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_files_creator_date
    ON files(creator_id, created_at DESC);

-- ============================================================================
-- JOB OUTPUT PERFORMANCE INDEXES
-- ============================================================================

-- Index for job output listings by project
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_jobs_project_output
    ON jobs(project_id, created_at DESC)
    WHERE output_file IS NOT NULL;

-- Index for job status queries (frequently used for filtering)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_jobs_status_created
    ON jobs(status, created_at DESC);

-- Index for user's jobs across projects
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_jobs_creator_date
    ON jobs(creator_id, created_at DESC);

-- ============================================================================
-- PROJECT PERFORMANCE INDEXES
-- ============================================================================

-- Index for project team membership (role inheritance)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_projects_team
    ON projects(team_id) WHERE team_id IS NOT NULL;

-- Index for archived projects (to exclude from active listings)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_projects_archived
    ON projects(is_archived, created_at DESC);

-- Index for public projects (category browsing)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_projects_public_category
    ON projects(category_id, created_at DESC)
    WHERE is_private = 0 AND is_archived = 0;

-- ============================================================================
-- TEAM ASSIGNMENT PERFORMANCE INDEXES
-- ============================================================================

-- Index for checking user's teams (already exists, but ensure it's there)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_team_assignments_user
    ON team_assignments(user_id);

-- Index for team membership lookups (role inheritance)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_team_members_team_user
    ON team_members(team_id, user_id);

-- ============================================================================
-- PARTIAL INDEXES FOR SPECIFIC QUERIES
-- ============================================================================

-- Index for failed jobs (for retry/debugging)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_jobs_failed
    ON jobs(project_id, created_at DESC)
    WHERE status = 'failed';

-- Index for completed jobs with outputs
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_jobs_completed_output
    ON jobs(project_id, created_at DESC, output_file_size)
    WHERE status = 'completed' AND output_file IS NOT NULL;

-- ============================================================================
-- TEXT SEARCH INDEXES (for future search improvements)
-- ============================================================================

-- GIN index for file name search (if PostgreSQL full-text search is used)
-- Commented out by default, uncomment if needed:
-- CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_files_name_search
--     ON files USING gin(to_tsvector('english', name));

-- CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_projects_name_search
--     ON projects USING gin(to_tsvector('english', name || ' ' || COALESCE(description, '')));

-- ============================================================================
-- STATISTICS UPDATE
-- ============================================================================

-- Analyze tables to update query planner statistics
ANALYZE files;
ANALYZE jobs;
ANALYZE projects;
ANALYZE team_assignments;
ANALYZE team_members;

-- ============================================================================
-- PERFORMANCE VALIDATION QUERIES
-- ============================================================================

-- These queries can be used to verify index usage:

-- 1. File listing for project (should use idx_files_project_details)
-- EXPLAIN ANALYZE
-- SELECT id, name, size, created_at, notes
-- FROM files
-- WHERE project_id = 1
-- ORDER BY created_at DESC;

-- 2. Job outputs for project (should use idx_jobs_project_output)
-- EXPLAIN ANALYZE
-- SELECT job_id, output_file, output_display_name, output_file_size, created_at
-- FROM jobs
-- WHERE project_id = 1 AND output_file IS NOT NULL
-- ORDER BY created_at DESC;

-- 3. User's all files (should use idx_files_creator_date)
-- EXPLAIN ANALYZE
-- SELECT id, name, size, created_at
-- FROM files
-- WHERE creator_id = 1
-- ORDER BY created_at DESC
-- LIMIT 50;

-- ============================================================================
-- INDEX SIZE REPORT
-- ============================================================================

-- Query to check index sizes (useful for monitoring)
SELECT
    schemaname,
    tablename,
    indexname,
    pg_size_pretty(pg_relation_size(indexrelid)) AS index_size,
    idx_scan AS times_used,
    idx_tup_read AS tuples_read
FROM pg_stat_user_indexes
WHERE schemaname = 'public'
  AND (tablename IN ('files', 'jobs', 'projects', 'team_assignments', 'team_members'))
ORDER BY pg_relation_size(indexrelid) DESC;

-- ============================================================================
-- ROLLBACK INSTRUCTIONS
-- ============================================================================

-- To rollback this migration:
-- DROP INDEX CONCURRENTLY IF EXISTS idx_files_project_details;
-- DROP INDEX CONCURRENTLY IF EXISTS idx_files_creator_date;
-- DROP INDEX CONCURRENTLY IF EXISTS idx_jobs_project_output;
-- DROP INDEX CONCURRENTLY IF EXISTS idx_jobs_status_created;
-- DROP INDEX CONCURRENTLY IF EXISTS idx_jobs_creator_date;
-- DROP INDEX CONCURRENTLY IF EXISTS idx_projects_team;
-- DROP INDEX CONCURRENTLY IF EXISTS idx_projects_archived;
-- DROP INDEX CONCURRENTLY IF EXISTS idx_projects_public_category;
-- DROP INDEX CONCURRENTLY IF EXISTS idx_team_assignments_user;
-- DROP INDEX CONCURRENTLY IF EXISTS idx_team_members_team_user;
-- DROP INDEX CONCURRENTLY IF EXISTS idx_jobs_failed;
-- DROP INDEX CONCURRENTLY IF EXISTS idx_jobs_completed_output;
