-- User-Defined Models Schema
-- This schema supports user-hosted computational models with secure Bridge API architecture

-- Table: user_models
-- Stores definitions of user-created computational models
CREATE TABLE IF NOT EXISTS user_models (
    id SERIAL PRIMARY KEY,
    model_id VARCHAR(16) UNIQUE NOT NULL,  -- Unique 16-char alphanumeric identifier
    user_id INTEGER NOT NULL,
    project_id INTEGER,  -- Optional: associate model with specific project
    model_name VARCHAR(255) NOT NULL,
    model_description TEXT,
    model_type VARCHAR(50) DEFAULT 'custom',  -- 'custom', 'python', 'docker', etc.

    -- Input/Output Configuration (JSON)
    input_schema JSONB NOT NULL DEFAULT '[]'::jsonb,  -- Array of input field definitions
    output_format VARCHAR(50) DEFAULT 'json',  -- Output file extension

    -- Visual Styling
    outline_color VARCHAR(20) DEFAULT 'gray',
    badge VARCHAR(50),
    badge_color VARCHAR(20),

    -- Compute & Billing
    credit_cost INTEGER DEFAULT 5,
    estimated_runtime_seconds INTEGER DEFAULT 60,

    -- Worker Configuration
    worker_endpoint_url TEXT,  -- Optional: for users hosting their own bridge
    requires_api_key BOOLEAN DEFAULT true,

    -- Status & Availability
    is_active BOOLEAN DEFAULT true,
    is_public BOOLEAN DEFAULT false,  -- Allow other users to use this model

    -- Metadata
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP,
    total_jobs_run INTEGER DEFAULT 0,

    -- Constraints
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
);

-- Table: user_model_api_keys
-- API keys specifically for Bridge API authentication (per-model or per-project)
CREATE TABLE IF NOT EXISTS user_model_api_keys (
    id SERIAL PRIMARY KEY,
    user_model_id INTEGER,
    project_id INTEGER,
    user_id INTEGER NOT NULL,

    -- API Key Components
    key_id VARCHAR(255) UNIQUE NOT NULL,
    key_hash VARCHAR(255) NOT NULL,
    key_prefix VARCHAR(20) NOT NULL,  -- First 8 chars for UI display

    -- Metadata
    name VARCHAR(255) NOT NULL,
    description TEXT,

    -- Security & Rate Limiting
    is_active BOOLEAN DEFAULT true,
    rate_limit_per_minute INTEGER DEFAULT 60,
    allowed_ip_addresses TEXT[],  -- Array of allowed IPs (optional)

    -- Usage Tracking
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP,
    total_requests INTEGER DEFAULT 0,

    -- Constraints
    FOREIGN KEY (user_model_id) REFERENCES user_models(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,

    -- Ensure at least one of model_id or project_id is set
    CHECK (user_model_id IS NOT NULL OR project_id IS NOT NULL)
);

-- Table: external_job_queue
-- Tracks jobs assigned to external (user-hosted) workers
CREATE TABLE IF NOT EXISTS external_job_queue (
    id SERIAL PRIMARY KEY,
    job_id INTEGER NOT NULL UNIQUE,
    user_model_id INTEGER NOT NULL,

    -- Queue Status
    status VARCHAR(50) DEFAULT 'pending',  -- 'pending', 'assigned', 'processing', 'completed', 'failed'
    assigned_at TIMESTAMP,
    completed_at TIMESTAMP,

    -- Worker Information
    worker_api_key_id INTEGER,
    worker_identifier VARCHAR(255),  -- IP or worker ID

    -- Retry & Timeout
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    timeout_seconds INTEGER DEFAULT 3600,

    -- Metadata
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Constraints
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE,
    FOREIGN KEY (user_model_id) REFERENCES user_models(id) ON DELETE CASCADE,
    FOREIGN KEY (worker_api_key_id) REFERENCES user_model_api_keys(id) ON DELETE SET NULL
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_user_models_model_id ON user_models(model_id);
CREATE INDEX IF NOT EXISTS idx_user_models_user_id ON user_models(user_id);
CREATE INDEX IF NOT EXISTS idx_user_models_project_id ON user_models(project_id);
CREATE INDEX IF NOT EXISTS idx_user_models_is_active ON user_models(is_active);
CREATE INDEX IF NOT EXISTS idx_user_models_is_public ON user_models(is_public);

CREATE INDEX IF NOT EXISTS idx_user_model_api_keys_user_model_id ON user_model_api_keys(user_model_id);
CREATE INDEX IF NOT EXISTS idx_user_model_api_keys_project_id ON user_model_api_keys(project_id);
CREATE INDEX IF NOT EXISTS idx_user_model_api_keys_user_id ON user_model_api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_user_model_api_keys_key_hash ON user_model_api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_user_model_api_keys_is_active ON user_model_api_keys(is_active);

CREATE INDEX IF NOT EXISTS idx_external_job_queue_job_id ON external_job_queue(job_id);
CREATE INDEX IF NOT EXISTS idx_external_job_queue_user_model_id ON external_job_queue(user_model_id);
CREATE INDEX IF NOT EXISTS idx_external_job_queue_status ON external_job_queue(status);
CREATE INDEX IF NOT EXISTS idx_external_job_queue_created_at ON external_job_queue(created_at);

-- Trigger to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_user_models_updated_at
    BEFORE UPDATE ON user_models
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_external_job_queue_updated_at
    BEFORE UPDATE ON external_job_queue
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();
