-- ================================================================
-- PHONOS NOTEBOOK SYSTEM - Database Migration
-- Migration: 006_notebook_system.sql
-- Description: Create tables for Jupyter-like notebook functionality
-- ================================================================

-- Notebooks table (main object, stored in projects)
CREATE TABLE IF NOT EXISTS notebooks (
    id SERIAL PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    creator_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    name VARCHAR(255) NOT NULL DEFAULT 'Untitled Notebook',
    description TEXT,
    kernel_type VARCHAR(50) DEFAULT 'python3',
    metadata JSONB DEFAULT '{}',
    cells JSONB DEFAULT '[]',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE
);

-- Notebook sessions (active kernel connections)
CREATE TABLE IF NOT EXISTS notebook_sessions (
    id SERIAL PRIMARY KEY,
    notebook_id INTEGER REFERENCES notebooks(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    container_id VARCHAR(100),
    session_token VARCHAR(100) UNIQUE NOT NULL,
    kernel_status VARCHAR(20) DEFAULT 'starting',
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    workspace_path TEXT,
    error_message TEXT,
    CONSTRAINT check_kernel_status CHECK (kernel_status IN ('starting', 'idle', 'busy', 'dead', 'disconnected', 'error'))
);

-- Notebook files (sub-filesystem for notebooks within projects)
CREATE TABLE IF NOT EXISTS notebook_files (
    id SERIAL PRIMARY KEY,
    notebook_id INTEGER REFERENCES notebooks(id) ON DELETE CASCADE,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    filename VARCHAR(255) NOT NULL,
    file_path TEXT NOT NULL,
    file_type VARCHAR(50),
    size_bytes BIGINT DEFAULT 0,
    mime_type VARCHAR(100),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(notebook_id, file_path)
);

-- Execution history (for audit and replay)
CREATE TABLE IF NOT EXISTS notebook_executions (
    id SERIAL PRIMARY KEY,
    session_id INTEGER REFERENCES notebook_sessions(id) ON DELETE CASCADE,
    cell_id VARCHAR(100) NOT NULL,
    execution_count INTEGER,
    source TEXT NOT NULL,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    status VARCHAR(20) DEFAULT 'running',
    outputs JSONB DEFAULT '[]',
    error_output TEXT,
    CONSTRAINT check_execution_status CHECK (status IN ('running', 'completed', 'error', 'interrupted'))
);

-- Session cleanup tracking
CREATE TABLE IF NOT EXISTS notebook_session_cleanup (
    id SERIAL PRIMARY KEY,
    session_id INTEGER REFERENCES notebook_sessions(id) ON DELETE CASCADE,
    cleanup_scheduled_at TIMESTAMP NOT NULL,
    cleanup_executed_at TIMESTAMP,
    workspace_deleted BOOLEAN DEFAULT FALSE,
    files_archived BOOLEAN DEFAULT FALSE
);

-- ================================================================
-- INDEXES
-- ================================================================

CREATE INDEX IF NOT EXISTS idx_notebooks_project_id ON notebooks(project_id);
CREATE INDEX IF NOT EXISTS idx_notebooks_creator_id ON notebooks(creator_id);
CREATE INDEX IF NOT EXISTS idx_notebooks_updated_at ON notebooks(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_notebooks_not_deleted ON notebooks(is_deleted) WHERE is_deleted = 0;

CREATE INDEX IF NOT EXISTS idx_notebook_sessions_notebook_id ON notebook_sessions(notebook_id);
CREATE INDEX IF NOT EXISTS idx_notebook_sessions_user_id ON notebook_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_notebook_sessions_container_id ON notebook_sessions(container_id);
CREATE INDEX IF NOT EXISTS idx_notebook_sessions_token ON notebook_sessions(session_token);
CREATE INDEX IF NOT EXISTS idx_notebook_sessions_active ON notebook_sessions(kernel_status, last_activity)
    WHERE kernel_status IN ('starting', 'idle', 'busy');

CREATE INDEX IF NOT EXISTS idx_notebook_files_notebook_id ON notebook_files(notebook_id);
CREATE INDEX IF NOT EXISTS idx_notebook_files_project_id ON notebook_files(project_id);

CREATE INDEX IF NOT EXISTS idx_notebook_executions_session_id ON notebook_executions(session_id);
CREATE INDEX IF NOT EXISTS idx_notebook_executions_cell_id ON notebook_executions(cell_id);

CREATE INDEX IF NOT EXISTS idx_notebook_cleanup_scheduled ON notebook_session_cleanup(cleanup_scheduled_at)
    WHERE cleanup_executed_at IS NULL;

-- ================================================================
-- FUNCTIONS
-- ================================================================

-- Function to update notebook timestamp
CREATE OR REPLACE FUNCTION update_notebook_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger for auto-updating notebook timestamp
DROP TRIGGER IF EXISTS notebook_updated_at_trigger ON notebooks;
CREATE TRIGGER notebook_updated_at_trigger
    BEFORE UPDATE ON notebooks
    FOR EACH ROW
    EXECUTE FUNCTION update_notebook_timestamp();

-- Function to update session last_activity
CREATE OR REPLACE FUNCTION update_session_activity()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE notebook_sessions
    SET last_activity = CURRENT_TIMESTAMP
    WHERE id = NEW.session_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger for auto-updating session activity on execution
DROP TRIGGER IF EXISTS execution_activity_trigger ON notebook_executions;
CREATE TRIGGER execution_activity_trigger
    AFTER INSERT ON notebook_executions
    FOR EACH ROW
    EXECUTE FUNCTION update_session_activity();

-- ================================================================
-- VIEWS
-- ================================================================

-- View for active sessions with user info
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

-- View for notebook statistics
CREATE OR REPLACE VIEW notebook_stats AS
SELECT
    n.id as notebook_id,
    n.name,
    n.project_id,
    n.creator_id,
    COUNT(DISTINCT ns.id) as total_sessions,
    COUNT(DISTINCT CASE WHEN ns.kernel_status IN ('idle', 'busy') THEN ns.id END) as active_sessions,
    COUNT(DISTINCT ne.id) as total_executions,
    MAX(ns.last_activity) as last_activity,
    COALESCE(SUM(nf.size_bytes), 0) as total_file_size,
    COUNT(DISTINCT nf.id) as file_count
FROM notebooks n
LEFT JOIN notebook_sessions ns ON n.id = ns.notebook_id
LEFT JOIN notebook_executions ne ON ns.id = ne.session_id
LEFT JOIN notebook_files nf ON n.id = nf.notebook_id
WHERE (n.is_deleted = 0 OR n.is_deleted IS NULL)
GROUP BY n.id, n.name, n.project_id, n.creator_id;

-- ================================================================
-- INITIAL DATA
-- ================================================================

-- No initial data needed

-- ================================================================
-- ANALYZE
-- ================================================================

ANALYZE notebooks;
ANALYZE notebook_sessions;
ANALYZE notebook_files;
ANALYZE notebook_executions;
