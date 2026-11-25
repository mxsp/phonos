-- ================================================================
-- PHONOS EDUCATION SYSTEM - Database Migration
-- Migration: 022_education_system.sql
-- Description: Education mode — a third project type ('education')
--              that turns the Notebook IDE into a WebWork-style
--              assignment platform. A teacher (project owner, blue)
--              creates assignments inside notebooks; invited students
--              (red) solve problems with submit / max-attempts /
--              time-limits and auto + manual grading.
--
-- NOTE: migrations/*.sql are NOT auto-run on boot. The schema below
-- is ALSO folded into production/database.py _create_schema() and
-- _apply_legacy_migrations() so it self-applies for both fresh
-- installs (PostgreSQL + SQLite/test) and existing databases. This
-- file exists for review / manual application only.
-- ================================================================

-- ================================================================
-- 1. PROJECT TYPE COLUMN
-- ================================================================
-- Stores the project kind explicitly. Existing rows are backfilled
-- via the inference logic already used by the API (legacy/private/team).
-- 'education' is the new third type introduced here.
ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS project_type TEXT NOT NULL DEFAULT 'legacy';

-- Backfill: any project still on the default 'legacy' that clearly
-- belongs to a personal workspace stays 'legacy'; team-scoped ones
-- become 'team'. (The application layer also normalises this on read.)
UPDATE projects
   SET project_type = 'team'
 WHERE project_type = 'legacy'
   AND team_id IS NOT NULL
   AND team_id NOT IN (SELECT id FROM teams WHERE slug = 'personal');

CREATE INDEX IF NOT EXISTS idx_projects_type ON projects(project_type);

-- ================================================================
-- 2. USER EDUCATION SETTINGS (the per-user flag)
-- ================================================================
-- One row per user. education_enabled = 1 lets that user create
-- 'education' projects as a teacher and exposes the education
-- surfaces in the UI. Self-service (any logged-in user can flip it).
CREATE TABLE IF NOT EXISTS user_education_settings (
    id                    SERIAL PRIMARY KEY,
    user_id               INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    education_enabled     INTEGER NOT NULL DEFAULT 0,         -- 0/1
    default_late_policy   TEXT    NOT NULL DEFAULT 'none',   -- none|penalty|reject
    default_time_limit_minutes INTEGER,                       -- nullable countdown default
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_user_education_settings_user ON user_education_settings(user_id);

-- ================================================================
-- 3. ASSIGNMENTS (a notebook becomes an assignment)
-- ================================================================
CREATE TABLE IF NOT EXISTS education_assignments (
    id                          SERIAL PRIMARY KEY,
    notebook_id                 INTEGER NOT NULL UNIQUE REFERENCES notebooks(id) ON DELETE CASCADE,
    project_id                  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title                       TEXT NOT NULL,
    description                 TEXT,
    open_at                     TIMESTAMP,                       -- homework window start
    close_at                    TIMESTAMP,                       -- homework window end
    time_limit_minutes          INTEGER,                         -- per-student countdown (nullable)
    late_policy                 TEXT    NOT NULL DEFAULT 'none', -- none|penalty|reject
    max_submissions_per_problem INTEGER NOT NULL DEFAULT 1,
    total_points                REAL    NOT NULL DEFAULT 0,
    published                   INTEGER NOT NULL DEFAULT 0,      -- 0 = draft, 1 = visible to students
    created_by                  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_edu_assignments_project ON education_assignments(project_id);
CREATE INDEX IF NOT EXISTS idx_edu_assignments_notebook ON education_assignments(notebook_id);
CREATE INDEX IF NOT EXISTS idx_edu_assignments_published ON education_assignments(published, open_at, close_at);

-- ================================================================
-- 4. PROBLEMS (cells inside an assignment notebook)
-- ================================================================
-- config JSONB holds type-specific grading config:
--   numeric : { correct_answer_enc, tolerance, points }
--   formula : { correct_answer_enc, points }            (Wolfram symbolic compare)
--   mc      : { options[], correct_index_enc, points }
--   code    : { test_cases[], points }                  (hidden tests run in kernel)
--   text    : { points, max_words }                     (manual grading)
--   file    : { allowed_ext[], max_size, points }       (manual grading)
-- *_enc fields are AES-256-GCM ciphertext (host ENCRYPTION_KEY) and
-- are NEVER sent to the student frontend.
CREATE TABLE IF NOT EXISTS assignment_problems (
    id              SERIAL PRIMARY KEY,
    assignment_id   INTEGER NOT NULL REFERENCES education_assignments(id) ON DELETE CASCADE,
    cell_id         TEXT NOT NULL,            -- links to a notebook cell id
    problem_type    TEXT NOT NULL,            -- code|numeric|formula|mc|text|file
    config          JSONB NOT NULL DEFAULT '{}',
    points          REAL    NOT NULL DEFAULT 1,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(assignment_id, cell_id)
);

CREATE INDEX IF NOT EXISTS idx_assignment_problems_assignment ON assignment_problems(assignment_id);

-- ================================================================
-- 5. SUBMISSIONS (one row per attempt per problem per student)
-- ================================================================
CREATE TABLE IF NOT EXISTS assignment_submissions (
    id                  SERIAL PRIMARY KEY,
    assignment_id       INTEGER NOT NULL REFERENCES education_assignments(id) ON DELETE CASCADE,
    problem_id          INTEGER NOT NULL REFERENCES assignment_problems(id) ON DELETE CASCADE,
    student_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    attempt_number      INTEGER NOT NULL DEFAULT 1,
    answer              TEXT,                 -- student answer / code / file ref
    auto_score          REAL,                 -- set by auto-grader (nullable for manual)
    final_score         REAL,                 -- post-override / manual grade
    status              TEXT NOT NULL DEFAULT 'pending', -- pending|graded|locked|overdue|autograded
    submitted_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    graded_by           INTEGER REFERENCES users(id) ON DELETE SET NULL,
    feedback            TEXT,
    time_started        TIMESTAMP,            -- when the student opened the countdown
    time_remaining_sec  INTEGER,              -- remaining seconds at last interaction
    is_correct          BOOLEAN,              -- raw correctness before late-penalty adjustment
    UNIQUE(assignment_id, problem_id, student_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS idx_assignment_submissions_assignment ON assignment_submissions(assignment_id);
CREATE INDEX IF NOT EXISTS idx_assignment_submissions_student ON assignment_submissions(student_id);
CREATE INDEX IF NOT EXISTS idx_assignment_submissions_problem_student ON assignment_submissions(problem_id, student_id, attempt_number DESC);

-- ================================================================
-- 6. GRADEBOOK (aggregate per student per assignment)
-- ================================================================
CREATE TABLE IF NOT EXISTS assignment_grades (
    id              SERIAL PRIMARY KEY,
    assignment_id   INTEGER NOT NULL REFERENCES education_assignments(id) ON DELETE CASCADE,
    student_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    total_score     REAL NOT NULL DEFAULT 0,
    max_score       REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'in_progress', -- in_progress|submitted|graded|overdue
    time_started    TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(assignment_id, student_id)
);

CREATE INDEX IF NOT EXISTS idx_assignment_grades_assignment ON assignment_grades(assignment_id);
CREATE INDEX IF NOT EXISTS idx_assignment_grades_student ON assignment_grades(student_id);

-- ================================================================
-- 7. TRIGGERS
-- ================================================================
CREATE OR REPLACE FUNCTION update_user_education_settings_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS user_education_settings_updated_at_trigger ON user_education_settings;
CREATE TRIGGER user_education_settings_updated_at_trigger
    BEFORE UPDATE ON user_education_settings
    FOR EACH ROW
    EXECUTE FUNCTION update_user_education_settings_timestamp();

CREATE OR REPLACE FUNCTION update_education_assignment_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS education_assignments_updated_at_trigger ON education_assignments;
CREATE TRIGGER education_assignments_updated_at_trigger
    BEFORE UPDATE ON education_assignments
    FOR EACH ROW
    EXECUTE FUNCTION update_education_assignment_timestamp();

-- ================================================================
-- 8. ANALYZE
-- ================================================================
ANALYZE projects;
ANALYZE user_education_settings;
ANALYZE education_assignments;
ANALYZE assignment_problems;
ANALYZE assignment_submissions;
ANALYZE assignment_grades;
