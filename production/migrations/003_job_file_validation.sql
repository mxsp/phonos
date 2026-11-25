-- Migration 003: Job Input File Validation
-- Ensures job input files belong to the same project as the job
-- Date: 2026-01-06

-- ============================================================================
-- VALIDATION FUNCTION: Check job input file belongs to same project
-- ============================================================================

CREATE OR REPLACE FUNCTION validate_job_input_file_project()
RETURNS TRIGGER AS $$
DECLARE
    file_project_id INTEGER;
BEGIN
    -- Only validate if input_file_id is specified
    IF NEW.input_file_id IS NULL THEN
        RETURN NEW;
    END IF;

    -- Get the project_id of the input file
    SELECT project_id INTO file_project_id
    FROM files
    WHERE id = NEW.input_file_id;

    -- Check if file exists
    IF file_project_id IS NULL THEN
        RAISE EXCEPTION 'Input file with ID % does not exist', NEW.input_file_id;
    END IF;

    -- Validate that file belongs to same project as job
    IF file_project_id != NEW.project_id THEN
        RAISE EXCEPTION 'Input file (project %) must belong to same project as job (project %)',
            file_project_id, NEW.project_id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- CREATE TRIGGER
-- ============================================================================

-- Drop existing trigger if it exists
DROP TRIGGER IF EXISTS jobs_input_file_validation ON jobs;

-- Create trigger to validate on INSERT and UPDATE
CREATE TRIGGER jobs_input_file_validation
    BEFORE INSERT OR UPDATE ON jobs
    FOR EACH ROW
    EXECUTE FUNCTION validate_job_input_file_project();

-- ============================================================================
-- PERFORMANCE INDEXES
-- ============================================================================

-- Index for input file lookups (used by validation trigger)
CREATE INDEX IF NOT EXISTS idx_jobs_input_file
    ON jobs(input_file_id) WHERE input_file_id IS NOT NULL;

-- Index for job project + status queries (general performance)
CREATE INDEX IF NOT EXISTS idx_jobs_project_status
    ON jobs(project_id, status);

-- ============================================================================
-- DATA INTEGRITY CHECK
-- ============================================================================

-- Check for existing violations before applying trigger
-- This query finds jobs with input files from different projects
DO $$
DECLARE
    violation_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO violation_count
    FROM jobs j
    INNER JOIN files f ON j.input_file_id = f.id
    WHERE j.input_file_id IS NOT NULL
      AND j.project_id != f.project_id;

    IF violation_count > 0 THEN
        RAISE WARNING 'Found % existing job(s) with input files from different projects. These will need to be fixed manually.', violation_count;

        -- Log the violations for manual review
        RAISE NOTICE 'Violations:';
        FOR rec IN
            SELECT j.job_id, j.project_id as job_project, f.project_id as file_project, f.id as file_id
            FROM jobs j
            INNER JOIN files f ON j.input_file_id = f.id
            WHERE j.input_file_id IS NOT NULL
              AND j.project_id != f.project_id
        LOOP
            RAISE NOTICE 'Job % (project %) references file % (project %)',
                rec.job_id, rec.job_project, rec.file_id, rec.file_project;
        END LOOP;
    END IF;
END $$;

-- ============================================================================
-- ROLLBACK INSTRUCTIONS
-- ============================================================================

-- To rollback this migration:
-- DROP TRIGGER IF EXISTS jobs_input_file_validation ON jobs;
-- DROP FUNCTION IF EXISTS validate_job_input_file_project();
-- DROP INDEX IF EXISTS idx_jobs_input_file;
-- DROP INDEX IF EXISTS idx_jobs_project_status;
