-- Migration 020: Per-user Quantum execution settings + IBM Quantum credentials
--
-- Quantum Settings surface (Account Settings + Notebook Settings Panel) lets a
-- user pick execution mode (simulator / real hardware), credential source
-- (platform-managed / personal IBM Quantum key), preferred backend, and
-- advanced session options WITHOUT editing notebook code.
--
-- Two tables:
--   user_quantum_settings   — non-secret per-user preferences (one row per user)
--   user_ibm_credentials    — personal IBM Quantum API key (AES-256-GCM enc),
--                              never returned to the frontend / logs / APIs.
--
-- Admin / platform-managed credentials live in the host .env
-- (IBM_QUANTUM_API_KEY) and are NEVER stored in these tables.

CREATE TABLE IF NOT EXISTS user_quantum_settings (
    id                    SERIAL PRIMARY KEY,
    user_id               INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    execution_mode        TEXT    NOT NULL DEFAULT 'simulator',   -- simulator | hardware
    credential_source    TEXT    NOT NULL DEFAULT 'platform',    -- platform | personal
    preferred_backend     TEXT    NOT NULL DEFAULT 'automatic',   -- automatic | ibm_<name>
    auto_start_session    INTEGER NOT NULL DEFAULT 0,             -- 0 = lazy (spec default)
    session_timeout_sec   INTEGER NOT NULL DEFAULT 60,            -- 30..300
    last_error            TEXT,
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_user_quantum_settings_user ON user_quantum_settings (user_id);

CREATE TABLE IF NOT EXISTS user_ibm_credentials (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    api_key_enc         TEXT    NOT NULL,                         -- AES-256-GCM (host ENCRYPTION_KEY)
    instance_crn        TEXT,                                     -- optional, not secret
    preferred_backend   TEXT,                                     -- optional override
    last_verified_at    TIMESTAMP,
    last_error          TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_user_ibm_creds_user ON user_ibm_credentials (user_id);