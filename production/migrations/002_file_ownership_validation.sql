-- Migration 002: File Ownership Validation
-- Ensures file creators have proper project access before uploading
-- Date: 2026-01-06

-- ============================================================================
-- VALIDATION FUNCTION: Check file creator has project access
-- ============================================================================

CREATE OR REPLACE FUNCTION validate_file_creator_has_project_access()
RETURNS TRIGGER AS $$
DECLARE
    user_role TEXT;
    team_role TEXT;
    project_team_id INTEGER;
BEGIN
    -- Get the project's team_id
    SELECT team_id INTO project_team_id
    FROM projects
    WHERE id = NEW.project_id;

    -- Check if user has direct project assignment
    SELECT role INTO user_role
    FROM project_assignments
    WHERE project_id = NEW.project_id
    AND user_id = NEW.creator_id;

    -- If direct assignment exists, validate role
    IF user_role IS NOT NULL THEN
        IF user_role IN ('Project Manager', 'Project Contributor') THEN
            RETURN NEW;
        ELSE
            RAISE EXCEPTION 'User must have Project Manager or Project Contributor role to upload files. Current role: %', user_role;
        END IF;
    END IF;

    -- If no direct assignment, check team role inheritance
    IF project_team_id IS NOT NULL THEN
        SELECT role INTO team_role
        FROM team_assignments
        WHERE team_id = project_team_id
        AND user_id = NEW.creator_id;

        -- Team owners/admins inherit Project Manager role
        -- Team members inherit Project Contributor role
        -- Team billing inherits Project Viewer role (read-only, cannot upload)
        IF team_role IN ('owner', 'admin', 'member') THEN
            RETURN NEW;
        END IF;
    END IF;

    -- If we reach here, user has no valid access
    RAISE EXCEPTION 'User % does not have permission to upload files to project %. User must be a project member with Contributor or Manager role.',
        NEW.creator_id, NEW.project_id;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- CREATE TRIGGER
-- ============================================================================

-- Drop existing trigger if it exists
DROP TRIGGER IF EXISTS files_creator_validation ON files;

-- Create trigger to validate on INSERT
CREATE TRIGGER files_creator_validation
    BEFORE INSERT ON files
    FOR EACH ROW
    EXECUTE FUNCTION validate_file_creator_has_project_access();

-- ============================================================================
-- PERFORMANCE INDEXES
-- ============================================================================

-- Index for file creator + project lookups (used by validation trigger)
CREATE INDEX IF NOT EXISTS idx_files_project_creator
    ON files(project_id, creator_id);

-- Index for file path lookups (used by path resolution)
CREATE INDEX IF NOT EXISTS idx_files_path
    ON files(path) WHERE path IS NOT NULL;

-- Index for project assignments lookup (speeds up validation)
CREATE INDEX IF NOT EXISTS idx_project_assignments_user_project
    ON project_assignments(user_id, project_id);

-- Index for team assignments lookup (for role inheritance)
CREATE INDEX IF NOT EXISTS idx_team_assignments_user_team
    ON team_assignments(user_id, team_id);

-- ============================================================================
-- ROLLBACK INSTRUCTIONS
-- ============================================================================

-- To rollback this migration:
-- DROP TRIGGER IF EXISTS files_creator_validation ON files;
-- DROP FUNCTION IF EXISTS validate_file_creator_has_project_access();
-- DROP INDEX IF EXISTS idx_files_project_creator;
-- DROP INDEX IF EXISTS idx_files_path;
-- DROP INDEX IF EXISTS idx_project_assignments_user_project;
-- DROP INDEX IF EXISTS idx_team_assignments_user_team;
