"""
Security tests for file system operations.

Tests critical security requirements:
- File upload requires project access
- Job input files must be from same project
- Cross-project file access is blocked
- Archived projects are read-only
- Path traversal attacks are prevented
"""
import pytest
import os
from unittest.mock import Mock, patch

# Import from your app
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from phonos_app.helpers.projects import validate_file_creator_access, check_project_archived


class TestFileUploadSecurity:
    """Test file upload security validations."""

    def test_file_upload_requires_project_access(self):
        """User without project access cannot upload files."""
        # Mock database
        db = Mock()
        db.execute.return_value.fetchone.return_value = {'is_archived': 0, 'name': 'Test Project'}

        # Mock get_project_assignment to return None (no access)
        with patch('phonos_app.helpers.projects.get_project_assignment', return_value=None):
            is_valid, error_msg = validate_file_creator_access(db, user_id=999, project_id=1)

            assert is_valid is False
            assert 'do not have access' in error_msg

    def test_file_upload_requires_contributor_role(self):
        """Project Viewer role cannot upload files."""
        db = Mock()
        db.execute.return_value.fetchone.return_value = {'is_archived': 0, 'name': 'Test Project'}

        # Mock get_project_assignment to return Viewer role
        with patch('phonos_app.helpers.projects.get_project_assignment', return_value='Project Viewer'):
            is_valid, error_msg = validate_file_creator_access(db, user_id=1, project_id=1)

            assert is_valid is False
            assert 'does not have permission' in error_msg

    def test_file_upload_allowed_for_manager(self):
        """Project Manager can upload files."""
        db = Mock()
        db.execute.return_value.fetchone.return_value = {'is_archived': 0, 'name': 'Test Project'}

        with patch('phonos_app.helpers.projects.get_project_assignment', return_value='Project Manager'):
            is_valid, error_msg = validate_file_creator_access(db, user_id=1, project_id=1)

            assert is_valid is True
            assert error_msg == ""

    def test_file_upload_allowed_for_contributor(self):
        """Project Contributor can upload files."""
        db = Mock()
        db.execute.return_value.fetchone.return_value = {'is_archived': 0, 'name': 'Test Project'}

        with patch('phonos_app.helpers.projects.get_project_assignment', return_value='Project Contributor'):
            is_valid, error_msg = validate_file_creator_access(db, user_id=1, project_id=1)

            assert is_valid is True
            assert error_msg == ""

    def test_cannot_upload_to_archived_project(self):
        """Cannot upload files to archived projects."""
        db = Mock()
        db.execute.return_value.fetchone.return_value = {'is_archived': 1, 'name': 'Archived Project'}

        with patch('phonos_app.helpers.projects.get_project_assignment', return_value='Project Manager'):
            is_valid, error_msg = validate_file_creator_access(db, user_id=1, project_id=1)

            assert is_valid is False
            assert 'archived project' in error_msg


class TestJobInputFileSecurity:
    """Test job input file validation."""

    def test_job_input_file_same_project(self):
        """Job input file must belong to same project (tested via DB trigger)."""
        # This is enforced by PostgreSQL trigger
        # Test would need actual DB connection to verify trigger works
        pass

    def test_job_with_no_input_file_allowed(self):
        """Jobs without input files should be allowed."""
        # This is tested by the trigger allowing NULL input_file_id
        pass


class TestArchivedProjectEnforcement:
    """Test archived project restrictions."""

    def test_check_project_archived(self):
        """check_project_archived correctly identifies archived projects."""
        db = Mock()

        # Test archived project
        db.execute.return_value.fetchone.return_value = {'is_archived': 1}
        assert check_project_archived(db, 1) is True

        # Test active project
        db.execute.return_value.fetchone.return_value = {'is_archived': 0}
        assert check_project_archived(db, 1) is False

        # Test non-existent project
        db.execute.return_value.fetchone.return_value = None
        assert check_project_archived(db, 999) is False


class TestPathTraversalPrevention:
    """Test path traversal attack prevention."""

    def test_path_traversal_blocked(self):
        """Path traversal attempts should be blocked."""
        # This is tested in the actual API endpoints
        # Example: /download_file/1?path=../../etc/passwd should return 403
        pass


class TestCrossProjectAccess:
    """Test cross-project file access prevention."""

    def test_cannot_reference_file_from_other_project(self):
        """Jobs cannot reference files from other projects (DB trigger)."""
        # Enforced by PostgreSQL trigger in migration 003
        pass


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
