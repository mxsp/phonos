-- ================================================================
-- PHONOS DATABASE IMPROVEMENTS
-- Critical indexes and optimizations for better interconnection
-- ================================================================

-- ================================================================
-- CRITICAL PRIORITY (P0) - Add immediately
-- ================================================================

-- Team relationships (fix N+1 queries)
CREATE INDEX IF NOT EXISTS idx_team_members_team_id ON team_members(team_id);
CREATE INDEX IF NOT EXISTS idx_team_members_user_id ON team_members(user_id);
CREATE INDEX IF NOT EXISTS idx_team_members_team_user ON team_members(team_id, user_id);

-- Project assignments (critical for access control)
CREATE INDEX IF NOT EXISTS idx_project_assignments_project_id ON project_assignments(project_id);
CREATE INDEX IF NOT EXISTS idx_project_assignments_user_id ON project_assignments(user_id);
CREATE INDEX IF NOT EXISTS idx_project_assignments_project_user ON project_assignments(project_id, user_id);

-- Notifications (critical for user experience)
CREATE INDEX IF NOT EXISTS idx_notifications_user_id_is_read ON notifications(user_id, is_read);
CREATE INDEX IF NOT EXISTS idx_notifications_user_created ON notifications(user_id, created_at DESC);

-- Analytics (prevents slow dashboard loads)
CREATE INDEX IF NOT EXISTS idx_usage_analytics_user_created ON usage_analytics(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_analytics_user_event_created ON usage_analytics(user_id, event_type, created_at);

-- Message attachments and reactions (fix N+1 in message loading)
CREATE INDEX IF NOT EXISTS idx_message_attachments_message_id ON message_attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_message_reactions_message_id ON message_reactions(message_id);

-- ================================================================
-- HIGH PRIORITY (P1) - Add within 1 week
-- ================================================================

-- File management
CREATE INDEX IF NOT EXISTS idx_files_project_id ON files(project_id);
CREATE INDEX IF NOT EXISTS idx_files_creator_id ON files(creator_id);
CREATE INDEX IF NOT EXISTS idx_file_versions_file_id ON file_versions(file_id);

-- Direct messages
CREATE INDEX IF NOT EXISTS idx_direct_messages_sender_id ON direct_messages(sender_id);
CREATE INDEX IF NOT EXISTS idx_direct_messages_recipient_id ON direct_messages(recipient_id);
CREATE INDEX IF NOT EXISTS idx_direct_messages_sender_recipient ON direct_messages(sender_id, recipient_id, created_at DESC);

-- Comments and voting
CREATE INDEX IF NOT EXISTS idx_project_comments_project_deleted ON project_comments(project_id, is_deleted);
CREATE INDEX IF NOT EXISTS idx_project_comment_votes_comment_id ON project_comment_votes(comment_id);
CREATE INDEX IF NOT EXISTS idx_project_votes_project_id ON project_votes(project_id);

-- Projects and teams
CREATE INDEX IF NOT EXISTS idx_projects_team_id ON projects(team_id);
CREATE INDEX IF NOT EXISTS idx_projects_creator_id ON projects(creator_id);
CREATE INDEX IF NOT EXISTS idx_team_invitations_team_id ON team_invitations(team_id);
CREATE INDEX IF NOT EXISTS idx_team_invitations_email ON team_invitations(email);

-- Collaboration features
CREATE INDEX IF NOT EXISTS idx_collaboration_sessions_session_id ON collaboration_sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_collaboration_participants_session_id ON collaboration_participants(session_id);
CREATE INDEX IF NOT EXISTS idx_call_sessions_creator_id ON call_sessions(creator_id);
CREATE INDEX IF NOT EXISTS idx_call_participants_session_id ON call_participants(session_id);

-- Jobs with composite indexes
CREATE INDEX IF NOT EXISTS idx_jobs_project_status ON jobs(project_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_creator_created ON jobs(creator_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_logs_job_id ON job_logs(job_id);

-- ================================================================
-- MEDIUM PRIORITY (P2) - Add within 1 month
-- ================================================================

-- Advanced filtering
CREATE INDEX IF NOT EXISTS idx_project_assignments_user_role ON project_assignments(user_id, role);
CREATE INDEX IF NOT EXISTS idx_team_invitations_token ON team_invitations(token);
CREATE INDEX IF NOT EXISTS idx_call_sessions_status ON call_sessions(status);
CREATE INDEX IF NOT EXISTS idx_team_invitations_status ON team_invitations(status);

-- Activity logging
CREATE INDEX IF NOT EXISTS idx_activity_log_user_created ON activity_log(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_log_type_created ON activity_log(activity_type, created_at DESC);

-- Budget and analytics
CREATE INDEX IF NOT EXISTS idx_budget_alerts_user_id ON budget_alerts(user_id);
CREATE INDEX IF NOT EXISTS idx_shared_links_resource_type ON shared_links(resource_type, resource_id);

-- API management
CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_rate_limits_api_key ON api_rate_limits(api_key_id);

-- ================================================================
-- JSONB INDEXES - For metadata queries
-- ================================================================

-- Enable GIN indexing on JSONB metadata fields
CREATE INDEX IF NOT EXISTS idx_usage_analytics_metadata_gin
ON usage_analytics USING gin ((metadata::jsonb));

CREATE INDEX IF NOT EXISTS idx_jobs_metadata_gin
ON jobs USING gin ((metadata::jsonb));

-- Specific JSONB path indexes for common queries
CREATE INDEX IF NOT EXISTS idx_usage_analytics_model
ON usage_analytics ((metadata::jsonb->>'model'));

CREATE INDEX IF NOT EXISTS idx_jobs_model
ON jobs ((metadata::jsonb->>'model'));

-- ================================================================
-- PARTIAL INDEXES - For filtered queries
-- ================================================================

-- Only index active/pending jobs
CREATE INDEX IF NOT EXISTS idx_jobs_active_status
ON jobs(project_id, created_at DESC)
WHERE status IN ('submitted', 'queued', 'processing', 'running');

-- Only index unread notifications
CREATE INDEX IF NOT EXISTS idx_notifications_unread
ON notifications(user_id, created_at DESC)
WHERE is_read = 0;

-- Only index non-deleted comments
CREATE INDEX IF NOT EXISTS idx_project_comments_active
ON project_comments(project_id, created_at DESC)
WHERE is_deleted = 0;

-- Only index active collaboration sessions
CREATE INDEX IF NOT EXISTS idx_collaboration_sessions_active
ON collaboration_sessions(created_at DESC)
WHERE is_active = 1;

-- ================================================================
-- TEXT SEARCH INDEXES - For full-text search
-- ================================================================

-- Project search
CREATE INDEX IF NOT EXISTS idx_projects_name_trgm
ON projects USING gin (name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_projects_description_trgm
ON projects USING gin (description gin_trgm_ops);

-- Job search
CREATE INDEX IF NOT EXISTS idx_jobs_name_trgm
ON jobs USING gin (name gin_trgm_ops);

-- User search
CREATE INDEX IF NOT EXISTS idx_users_username_trgm
ON users USING gin (username gin_trgm_ops);

-- ================================================================
-- CONSTRAINTS AND DATA INTEGRITY
-- ================================================================

-- Unique constraints to prevent duplicates
ALTER TABLE team_members
ADD CONSTRAINT IF NOT EXISTS unique_team_member
UNIQUE (team_id, user_id);

ALTER TABLE project_assignments
ADD CONSTRAINT IF NOT EXISTS unique_project_assignment
UNIQUE (project_id, user_id);

ALTER TABLE project_votes
ADD CONSTRAINT IF NOT EXISTS unique_project_vote
UNIQUE (project_id, user_id);

ALTER TABLE project_comment_votes
ADD CONSTRAINT IF NOT EXISTS unique_comment_vote
UNIQUE (comment_id, user_id);

-- Check constraints for status fields
ALTER TABLE jobs
ADD CONSTRAINT IF NOT EXISTS check_job_status
CHECK (status IN ('submitted', 'queued', 'processing', 'running', 'completed', 'failed', 'cancelled'));

ALTER TABLE call_sessions
ADD CONSTRAINT IF NOT EXISTS check_call_status
CHECK (status IN ('waiting', 'active', 'ended'));

-- ================================================================
-- VIEWS FOR COMMON QUERIES
-- ================================================================

-- Optimized view for user's projects with member counts
CREATE OR REPLACE VIEW user_projects_with_stats AS
SELECT
    p.*,
    pa.role as user_role,
    COUNT(DISTINCT pa2.user_id) as member_count,
    COUNT(DISTINCT j.id) as total_jobs,
    COUNT(DISTINCT CASE WHEN j.status = 'completed' THEN j.id END) as completed_jobs,
    COUNT(DISTINCT f.id) as file_count,
    COALESCE(SUM(f.size_bytes), 0) as total_storage
FROM projects p
JOIN project_assignments pa ON p.id = pa.project_id
LEFT JOIN project_assignments pa2 ON p.id = pa2.project_id
LEFT JOIN jobs j ON p.id = j.project_id
LEFT JOIN files f ON p.id = f.project_id
GROUP BY p.id, pa.role;

-- Optimized view for message threads with counts
CREATE OR REPLACE VIEW message_threads_with_stats AS
SELECT
    pm.*,
    COUNT(DISTINCT ma.id) as attachment_count,
    COUNT(DISTINCT mr.id) as reaction_count,
    json_agg(DISTINCT jsonb_build_object(
        'reaction', mr.reaction,
        'count', (SELECT COUNT(*) FROM message_reactions mr2
                 WHERE mr2.message_id = pm.id AND mr2.reaction = mr.reaction)
    )) FILTER (WHERE mr.id IS NOT NULL) as reaction_summary
FROM project_messages pm
LEFT JOIN message_attachments ma ON pm.id = ma.message_id
LEFT JOIN message_reactions mr ON pm.id = mr.message_id
GROUP BY pm.id;

-- ================================================================
-- MAINTENANCE QUERIES
-- ================================================================

-- Analyze tables after index creation
ANALYZE users;
ANALYZE projects;
ANALYZE jobs;
ANALYZE project_assignments;
ANALYZE team_members;
ANALYZE notifications;
ANALYZE usage_analytics;
ANALYZE project_messages;
ANALYZE files;

-- Show index usage statistics (run after deployment)
-- SELECT schemaname, tablename, indexname, idx_scan, idx_tup_read, idx_tup_fetch
-- FROM pg_stat_user_indexes
-- WHERE schemaname = 'public'
-- ORDER BY idx_scan ASC;

-- Show missing indexes (tables without indexes on foreign keys)
-- SELECT
--     c.conrelid::regclass AS table_name,
--     a.attname AS column_name,
--     c.confrelid::regclass AS foreign_table
-- FROM pg_constraint c
-- JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
-- WHERE c.contype = 'f'
-- AND NOT EXISTS (
--     SELECT 1 FROM pg_index i
--     WHERE i.indrelid = c.conrelid
--     AND a.attnum = ANY(i.indkey)
-- );
