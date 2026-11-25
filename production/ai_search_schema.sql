-- AI Search Conversations Schema
-- Tables for storing AI-powered search conversations

SET search_path TO public;

-- AI Conversations Table
CREATE TABLE IF NOT EXISTS public.ai_conversations (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ai_conversations_user_id ON ai_conversations(user_id);
CREATE INDEX IF NOT EXISTS idx_ai_conversations_updated_at ON ai_conversations(updated_at DESC);

-- AI Conversation Messages Table
CREATE TABLE IF NOT EXISTS public.ai_conversation_messages (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES ai_conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'model', 'assistant')),
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ai_conversation_messages_conversation_id ON ai_conversation_messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_ai_conversation_messages_created_at ON ai_conversation_messages(created_at);

-- Trigger to update the updated_at timestamp on ai_conversations
CREATE OR REPLACE FUNCTION update_ai_conversation_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE ai_conversations
    SET updated_at = CURRENT_TIMESTAMP
    WHERE id = NEW.conversation_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_update_ai_conversation_timestamp ON ai_conversation_messages;
CREATE TRIGGER trigger_update_ai_conversation_timestamp
    AFTER INSERT ON ai_conversation_messages
    FOR EACH ROW
    EXECUTE FUNCTION update_ai_conversation_timestamp();

-- Comments
COMMENT ON TABLE ai_conversations IS 'Stores AI search conversation threads';
COMMENT ON TABLE ai_conversation_messages IS 'Stores individual messages within AI conversations';
COMMENT ON COLUMN ai_conversation_messages.role IS 'Message role: user (user question) or model/assistant (AI response)';
