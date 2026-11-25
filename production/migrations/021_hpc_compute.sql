-- HPC compute foundation: named clusters of semi-external containerized
-- workers that pull tasks over HTTPS (phonos.hpc_<name>.compute(...)).
CREATE TABLE IF NOT EXISTS hpc_clusters (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    display_name TEXT,
    description TEXT,
    token_hash TEXT NOT NULL,
    capabilities JSONB DEFAULT '{}'::jsonb,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    last_seen TIMESTAMP,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hpc_tasks (
    id SERIAL PRIMARY KEY,
    task_uuid TEXT NOT NULL UNIQUE,
    cluster_id INTEGER NOT NULL REFERENCES hpc_clusters(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',
    code TEXT NOT NULL,
    args JSONB DEFAULT '{}'::jsonb,
    timeout_seconds INTEGER NOT NULL DEFAULT 300,
    result JSONB,
    error TEXT,
    stdout TEXT,
    duration_ms INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    finished_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_hpc_tasks_lease ON hpc_tasks (cluster_id, status, id);
CREATE INDEX IF NOT EXISTS idx_hpc_tasks_user ON hpc_tasks (user_id, created_at DESC);
