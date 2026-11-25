-- Migration: Add unique model_id to user_models table
-- Date: 2024-12-30
-- Description: Adds a unique 16-character alphanumeric model_id for scalable model identification

-- Step 1: Add the column as nullable first
ALTER TABLE user_models ADD COLUMN IF NOT EXISTS model_id VARCHAR(16);

-- Step 2: Generate unique model_ids for existing records
-- Using a combination of random characters for uniqueness
DO $$
DECLARE
    rec RECORD;
    new_id VARCHAR(16);
    chars TEXT := 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    i INT;
BEGIN
    FOR rec IN SELECT id FROM user_models WHERE model_id IS NULL LOOP
        -- Generate a unique 16-char ID
        LOOP
            new_id := '';
            FOR i IN 1..16 LOOP
                new_id := new_id || substr(chars, floor(random() * 62 + 1)::int, 1);
            END LOOP;

            -- Check if it's unique
            EXIT WHEN NOT EXISTS (SELECT 1 FROM user_models WHERE model_id = new_id);
        END LOOP;

        -- Update the record
        UPDATE user_models SET model_id = new_id WHERE id = rec.id;
    END LOOP;
END $$;

-- Step 3: Add NOT NULL constraint after all records have model_ids
ALTER TABLE user_models ALTER COLUMN model_id SET NOT NULL;

-- Step 4: Add unique constraint
ALTER TABLE user_models ADD CONSTRAINT user_models_model_id_unique UNIQUE (model_id);

-- Step 5: Create index for faster lookups
CREATE INDEX IF NOT EXISTS idx_user_models_model_id ON user_models(model_id);

-- Verify the migration
SELECT COUNT(*) as total_models, COUNT(DISTINCT model_id) as unique_model_ids FROM user_models;
