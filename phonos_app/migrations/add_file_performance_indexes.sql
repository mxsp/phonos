-- Migration: Add performance indexes for file and job operations
-- Created: 2025-11-25
-- Purpose: Optimize queries for large file sets and improve database interconnectivity

-- ============================================================================
-- FILE TABLE INDEXES
-- ============================================================================

-- Index on project_id for fast file lookups by project
CREATE INDEX IF NOT EXISTS idx_files_project_id ON files(project_id);

-- Index on creator_id for user file queries
CREATE INDEX IF NOT EXISTS idx_files_creator_id ON files(creator_id);

-- Composite index for project + creator queries (common pattern)
CREATE INDEX IF NOT EXISTS idx_files_project_creator ON files(project_id, creator_id);

-- Index on created_at for sorting/filtering by date
CREATE INDEX IF NOT EXISTS idx_files_created_at ON files(created_at DESC);

-- Index on size for storage quota calculations
CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);

-- Composite index for project files sorted by date (most common query)
CREATE INDEX IF NOT EXISTS idx_files_project_created ON files(project_id, created_at DESC);

-- ============================================================================
-- JOB TABLE INDEXES
-- ============================================================================

-- Index on project_id for job lookups by project
CREATE INDEX IF NOT EXISTS idx_jobs_project_id ON jobs(project_id);

-- Index on creator_id for user job queries
CREATE INDEX IF NOT EXISTS idx_jobs_creator_id ON jobs(creator_id);

-- Index on status for filtering active/completed jobs
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

-- Index on input_file_id for file-to-job relationship queries
CREATE INDEX IF NOT EXISTS idx_jobs_input_file_id ON jobs(input_file_id);

-- Composite index for project + status (common dashboard query)
CREATE INDEX IF NOT EXISTS idx_jobs_project_status ON jobs(project_id, status);

-- Composite index for user + status queries
CREATE INDEX IF NOT EXISTS idx_jobs_creator_status ON jobs(creator_id, status);

-- Index on created_at for job history queries
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);

-- ============================================================================
-- PROJECT ASSIGNMENTS INDEXES
-- ============================================================================

-- Index on user_id for user permission checks
CREATE INDEX IF NOT EXISTS idx_project_assignments_user ON project_assignments(user_id);

-- Index on project_id for project member lists
CREATE INDEX IF NOT EXISTS idx_project_assignments_project ON project_assignments(project_id);

-- Composite index for permission checks (most critical query)
CREATE INDEX IF NOT EXISTS idx_project_assignments_user_project ON project_assignments(user_id, project_id);

-- ============================================================================
-- PROJECTS TABLE INDEXES
-- ============================================================================

-- Index on team_id for team project queries
CREATE INDEX IF NOT EXISTS idx_projects_team_id ON projects(team_id);

-- Index on created_at for project sorting
CREATE INDEX IF NOT EXISTS idx_projects_created_at ON projects(created_at DESC);

-- ============================================================================
-- TEAMS TABLE INDEXES
-- ============================================================================

-- Index on created_at for team sorting
CREATE INDEX IF NOT EXISTS idx_teams_created_at ON teams(created_at DESC);

-- ============================================================================
-- TEAM MEMBERS INDEXES
-- ============================================================================

-- Index on user_id for user team queries
CREATE INDEX IF NOT EXISTS idx_team_members_user ON team_members(user_id);

-- Index on team_id for team member lists
CREATE INDEX IF NOT EXISTS idx_team_members_team ON team_members(team_id);

-- Composite index for membership checks
CREATE INDEX IF NOT EXISTS idx_team_members_user_team ON team_members(user_id, team_id);

-- ============================================================================
-- INTERCONNECTED DATABASE VIEWS FOR BETTER QUERY PERFORMANCE
-- ============================================================================

-- View: User file statistics (precomputed for dashboard)
CREATE OR REPLACE VIEW user_file_stats AS
SELECT
    u.id AS user_id,
    u.username,
    COUNT(DISTINCT f.id) AS total_files,
    COALESCE(SUM(f.size), 0) AS total_file_size,
    COUNT(DISTINCT f.project_id) AS projects_with_files,
    MAX(f.created_at) AS last_upload_at
FROM users u
LEFT JOIN files f ON f.creator_id = u.id
GROUP BY u.id, u.username;

-- View: Project file statistics
CREATE OR REPLACE VIEW project_file_stats AS
SELECT
    p.id AS project_id,
    p.name AS project_name,
    COUNT(DISTINCT f.id) AS total_files,
    COALESCE(SUM(f.size), 0) AS total_file_size,
    COUNT(DISTINCT f.creator_id) AS unique_uploaders,
    MAX(f.created_at) AS last_upload_at
FROM projects p
LEFT JOIN files f ON f.project_id = p.id
GROUP BY p.id, p.name;

-- View: Project job statistics
CREATE OR REPLACE VIEW project_job_stats AS
SELECT
    p.id AS project_id,
    p.name AS project_name,
    COUNT(DISTINCT j.id) AS total_jobs,
    COUNT(DISTINCT CASE WHEN j.status = 'completed' THEN j.id END) AS completed_jobs,
    COUNT(DISTINCT CASE WHEN j.status = 'failed' THEN j.id END) AS failed_jobs,
    COUNT(DISTINCT CASE WHEN j.status IN ('queued', 'running', 'processing') THEN j.id END) AS active_jobs,
    COALESCE(SUM(j.cost), 0) AS total_cost,
    COALESCE(SUM(CASE WHEN j.status = 'completed' THEN j.output_file_size ELSE 0 END), 0) AS total_output_size,
    MAX(j.created_at) AS last_job_at
FROM projects p
LEFT JOIN jobs j ON j.project_id = p.id
GROUP BY p.id, p.name;

-- View: File-to-job interconnections (shows which files are used in which jobs)
CREATE OR REPLACE VIEW file_job_relationships AS
SELECT
    f.id AS file_id,
    f.name AS file_name,
    f.project_id,
    f.creator_id,
    j.id AS job_id,
    j.job_id AS job_uuid,
    j.name AS job_name,
    j.status AS job_status,
    j.model AS job_model,
    j.created_at AS job_created_at
FROM files f
INNER JOIN jobs j ON j.input_file_id = f.id
ORDER BY j.created_at DESC;

-- View: Project activity summary (interconnected view of all project activity)
CREATE OR REPLACE VIEW project_activity_summary AS
SELECT
    p.id AS project_id,
    p.name AS project_name,
    p.team_id,
    t.name AS team_name,
    pfs.total_files,
    pfs.total_file_size,
    pjs.total_jobs,
    pjs.completed_jobs,
    pjs.failed_jobs,
    pjs.active_jobs,
    pjs.total_cost,
    pjs.total_output_size,
    COUNT(DISTINCT pa.user_id) AS team_members,
    GREATEST(
        COALESCE(pfs.last_upload_at, '1970-01-01'::timestamp),
        COALESCE(pjs.last_job_at, '1970-01-01'::timestamp)
    ) AS last_activity_at
FROM projects p
LEFT JOIN teams t ON t.id = p.team_id
LEFT JOIN project_file_stats pfs ON pfs.project_id = p.id
LEFT JOIN project_job_stats pjs ON pjs.project_id = p.id
LEFT JOIN project_assignments pa ON pa.project_id = p.id
GROUP BY
    p.id, p.name, p.team_id, t.name,
    pfs.total_files, pfs.total_file_size,
    pjs.total_jobs, pjs.completed_jobs, pjs.failed_jobs, pjs.active_jobs,
    pjs.total_cost, pjs.total_output_size,
    pfs.last_upload_at, pjs.last_job_at;

-- ============================================================================
-- MATERIALIZED VIEWS FOR EXPENSIVE QUERIES (OPTIONAL - UNCOMMENT IF NEEDED)
-- ============================================================================
-- Note: Materialized views cache results but need manual refresh
-- Useful for very large datasets (10k+ files/jobs)

-- CREATE MATERIALIZED VIEW IF NOT EXISTS mv_project_activity_summary AS
-- SELECT * FROM project_activity_summary;
--
-- CREATE UNIQUE INDEX ON mv_project_activity_summary(project_id);
--
-- To refresh: REFRESH MATERIALIZED VIEW CONCURRENTLY mv_project_activity_summary;

-- ============================================================================
-- QUERY PERFORMANCE FUNCTIONS
-- ============================================================================

-- Function: Get user's accessible files with permission check (optimized)
CREATE OR REPLACE FUNCTION get_user_accessible_files(p_user_id INTEGER, p_limit INTEGER DEFAULT 100, p_offset INTEGER DEFAULT 0)
RETURNS TABLE (
    file_id INTEGER,
    file_name TEXT,
    file_size BIGINT,
    file_created_at TIMESTAMP,
    project_id INTEGER,
    project_name TEXT,
    user_role TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        f.id,
        f.name,
        f.size,
        f.created_at,
        p.id,
        p.name,
        pa.role
    FROM files f
    INNER JOIN projects p ON p.id = f.project_id
    INNER JOIN project_assignments pa ON pa.project_id = p.id AND pa.user_id = p_user_id
    ORDER BY f.created_at DESC
    LIMIT p_limit
    OFFSET p_offset;
END;
$$ LANGUAGE plpgsql STABLE;

-- Function: Bulk delete files with storage update
CREATE OR REPLACE FUNCTION bulk_delete_files(p_file_ids INTEGER[], p_user_id INTEGER)
RETURNS TABLE (
    deleted_count INTEGER,
    freed_space BIGINT
) AS $$
DECLARE
    v_freed_space BIGINT;
    v_deleted_count INTEGER;
BEGIN
    -- Calculate total size to be freed
    SELECT COALESCE(SUM(size), 0)
    INTO v_freed_space
    FROM files
    WHERE id = ANY(p_file_ids) AND creator_id = p_user_id;

    -- Delete files
    DELETE FROM files
    WHERE id = ANY(p_file_ids) AND creator_id = p_user_id;

    GET DIAGNOSTICS v_deleted_count = ROW_COUNT;

    -- Update user storage
    IF v_freed_space > 0 THEN
        UPDATE users
        SET storage_used = GREATEST(0, storage_used - v_freed_space)
        WHERE id = p_user_id;
    END IF;

    RETURN QUERY SELECT v_deleted_count, v_freed_space;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- ANALYZE TABLES FOR QUERY PLANNER
-- ============================================================================

ANALYZE files;
ANALYZE jobs;
ANALYZE projects;
ANALYZE project_assignments;
ANALYZE teams;
ANALYZE team_members;

-- ============================================================================
-- PERFORMANCE MONITORING QUERIES (FOR ADMINS)
-- ============================================================================

-- To check index usage:
-- SELECT schemaname, tablename, indexname, idx_scan, idx_tup_read, idx_tup_fetch
-- FROM pg_stat_user_indexes
-- WHERE schemaname = 'public'
-- ORDER BY idx_scan DESC;

-- To check table sizes:
-- SELECT tablename, pg_size_pretty(pg_total_relation_size(tablename::regclass)) AS total_size
-- FROM pg_tables
-- WHERE schemaname = 'public'
-- ORDER BY pg_total_relation_size(tablename::regclass) DESC;
