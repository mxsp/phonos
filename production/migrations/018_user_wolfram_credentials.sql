-- Migration 018: Per-user Wolfram Cloud credentials
-- Non-admin users can register their own Wolfram Cloud account in the
-- Notebook IDE Settings panel. The admin keeps the global account in the
-- container's WOLFRAM_USERNAME / WOLFRAM_PASSWORD env vars.

CREATE TABLE IF NOT EXISTS user_wolfram_credentials (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    username_enc    TEXT    NOT NULL,
    password_enc    TEXT    NOT NULL,
    wolfram_id_email TEXT,
    last_verified_at TIMESTAMP,
    last_error      TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_user_wolfram_creds_user ON user_wolfram_credentials (user_id);
