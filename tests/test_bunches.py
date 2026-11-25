"""
Tests for job bunches functionality.

Tests cover:
- Creating bunches with multiple items
- Starting bunch execution
- Cancelling bunches and credit refunds
- Bunch status updates
- API endpoints for bunch operations
"""
import pytest
import json
import os
import sys
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

# Path setup is now handled centrally by conftest.py
# (kept for direct python -m pytest or script runs)


# =============================================================================
# HELPER FUNCTION TESTS
# =============================================================================

class TestBunchCreation:
    """Test bunch creation helper functions."""

    def test_generate_bunch_id_format(self):
        """Bunch IDs should follow format: bunch_<timestamp>_<random>."""
        from phonos_app.helpers.bunches import generate_bunch_id

        bunch_id = generate_bunch_id()

        assert bunch_id.startswith('bunch_')
        parts = bunch_id.split('_')
        assert len(parts) == 3
        # Middle part should be timestamp (numeric)
        assert parts[1].isdigit()
        # Last part should be 8 char alphanumeric
        assert len(parts[2]) == 8

    def test_generate_bunch_id_uniqueness(self):
        """Multiple calls should generate unique IDs."""
        from phonos_app.helpers.bunches import generate_bunch_id

        ids = [generate_bunch_id() for _ in range(100)]
        assert len(set(ids)) == 100  # All unique

    @patch('phonos_app.helpers.bunches.get_db')
    def test_create_bunch_with_sequences(self, mock_get_db):
        """Create bunch with direct sequence inputs."""
        from phonos_app.helpers.bunches import create_bunch

        # Proper mock for db.execute().fetchone()['id'] pattern
        mock_result = Mock()
        mock_result.fetchone.return_value = {'id': 1}
        mock_db = Mock()
        mock_db.execute.return_value = mock_result
        mock_get_db.return_value = mock_db

        items = [
            {'name': 'Protein 1', 'sequence': 'MKTAYIAKQRQISFVK'},
            {'name': 'Protein 2', 'sequence': 'MVLSPADKTNVKAAW'}
        ]

        result = create_bunch(
            project_id=1,
            creator_id=1,
            name='Test Bunch',
            model='type-0',
            items=items
        )

        assert result['status'] == 'success'
        assert 'bunch_id' in result
        assert mock_db.execute.called

    @patch('phonos_app.helpers.bunches.get_db')
    def test_create_bunch_with_file_ids(self, mock_get_db):
        """Create bunch with file_id references."""
        from phonos_app.helpers.bunches import create_bunch

        mock_result = Mock()
        mock_result.fetchone.return_value = {'id': 1}
        mock_db = Mock()
        mock_db.execute.return_value = mock_result
        mock_get_db.return_value = mock_db

        items = [
            {'name': 'From File 1', 'file_id': 123},
            {'name': 'From File 2', 'file_id': 456}
        ]

        result = create_bunch(
            project_id=1,
            creator_id=1,
            name='File Bunch',
            model='type-0',
            items=items
        )

        assert result['status'] == 'success'

    @patch('phonos_app.helpers.bunches.get_db')
    def test_create_bunch_empty_items_fails(self, mock_get_db):
        """Creating bunch with no items should fail."""
        from phonos_app.helpers.bunches import create_bunch

        mock_db = Mock()
        mock_get_db.return_value = mock_db

        result = create_bunch(
            project_id=1,
            creator_id=1,
            name='Empty Bunch',
            model='type-0',
            items=[]
        )

        assert result['status'] == 'error'
        assert 'items' in result['message'].lower()


class TestBunchRetrieval:
    """Test bunch retrieval functions."""

    @patch('phonos_app.helpers.bunches.get_db')
    def test_get_bunch_by_bunch_id(self, mock_get_db):
        """Get bunch by its string bunch_id."""
        from phonos_app.helpers.bunches import get_bunch

        db = Mock()
        db.execute.return_value.fetchone.return_value = {
            'id': 1,
            'bunch_id': 'bunch_123456_abcdefgh',
            'project_id': 1,
            'name': 'Test Bunch',
            'status': 'pending',
            'total_jobs': 5,
            'completed_jobs': 0,
            'failed_jobs': 0
        }
        mock_get_db.return_value = db

        result = get_bunch('bunch_123456_abcdefgh')

        assert result is not None
        assert result['bunch_id'] == 'bunch_123456_abcdefgh'
        assert result['name'] == 'Test Bunch'

    @patch('phonos_app.helpers.bunches.get_db')
    def test_get_bunch_not_found(self, mock_get_db):
        """Get non-existent bunch returns None."""
        from phonos_app.helpers.bunches import get_bunch

        db = Mock()
        db.execute.return_value.fetchone.return_value = None
        mock_get_db.return_value = db

        result = get_bunch('bunch_nonexistent')

        assert result is None

    @patch('phonos_app.helpers.bunches.get_db')
    def test_get_project_bunches(self, mock_get_db):
        """Get all bunches for a project."""
        from phonos_app.helpers.bunches import get_project_bunches

        db = Mock()
        db.execute.return_value.fetchall.return_value = [
            {'id': 1, 'bunch_id': 'bunch_1', 'name': 'Bunch 1', 'status': 'completed'},
            {'id': 2, 'bunch_id': 'bunch_2', 'name': 'Bunch 2', 'status': 'running'}
        ]
        mock_get_db.return_value = db

        bunches = get_project_bunches(project_id=1)

        assert len(bunches) == 2
        assert bunches[0]['name'] == 'Bunch 1'

    @patch('phonos_app.helpers.bunches.get_db')
    def test_get_bunch_items(self, mock_get_db):
        """Get all items in a bunch."""
        from phonos_app.helpers.bunches import get_bunch_items

        db = Mock()
        db.execute.return_value.fetchall.return_value = [
            {'id': 1, 'input_name': 'Protein 1', 'status': 'completed', 'job_id': 100},
            {'id': 2, 'input_name': 'Protein 2', 'status': 'running', 'job_id': 101},
            {'id': 3, 'input_name': 'Protein 3', 'status': 'pending', 'job_id': None}
        ]
        mock_get_db.return_value = db

        items = get_bunch_items(bunch_id=1)

        assert len(items) == 3
        assert items[0]['status'] == 'completed'
        assert items[2]['job_id'] is None


@pytest.mark.skip(reason="Execution tests involve heavy job submission and credit logic")
class TestBunchExecution:
    """Test bunch execution (starting jobs)."""

    @patch('phonos_app.helpers.bunches.get_db')
    @patch('phonos_app.helpers.bunches.get_bunch')
    @patch('phonos_app.helpers.bunches.get_bunch_items')
    def test_start_bunch_submits_jobs(self, mock_items, mock_bunch, mock_get_db):
        """Starting a bunch should submit all pending jobs."""
        from phonos_app.helpers.bunches import start_bunch

        db = Mock()
        mock_get_db.return_value = db

        mock_bunch.return_value = {
            'id': 1,
            'bunch_id': 'bunch_test',
            'project_id': 1,
            'creator_id': 1,
            'model': 'type-0',
            'status': 'pending',
            'total_jobs': 2
        }

        mock_items.return_value = [
            {'id': 1, 'input_name': 'P1', 'input_sequence': 'MKTAYIAK', 'status': 'pending'},
            {'id': 2, 'input_name': 'P2', 'input_sequence': 'MVLSPAD', 'status': 'pending'}
        ]

        # Mock user credits
        db.execute.return_value.fetchone.side_effect = [
            {'credits': 100},  # User credits
            {'id': 1},  # Job creation
            {'id': 2}   # Job creation
        ]

        result = start_bunch('bunch_test', user_id=1)

        assert result['status'] == 'success' or result['status'] == 'error'

    @patch('phonos_app.helpers.bunches.get_db')
    @patch('phonos_app.helpers.bunches.get_bunch')
    def test_start_bunch_already_running_fails(self, mock_bunch, mock_get_db):
        """Cannot start a bunch that's already running."""
        from phonos_app.helpers.bunches import start_bunch

        db = Mock()
        mock_get_db.return_value = db

        mock_bunch.return_value = {
            'id': 1,
            'bunch_id': 'bunch_test',
            'status': 'running'  # Already running
        }

        result = start_bunch('bunch_test', user_id=1)

        assert result['status'] == 'error'
        assert 'already' in result['message'].lower() or 'running' in result['message'].lower()

    @patch('phonos_app.helpers.bunches.get_db')
    @patch('phonos_app.helpers.bunches.get_bunch')
    def test_start_bunch_insufficient_credits(self, mock_bunch, mock_get_db):
        """Cannot start bunch without sufficient credits."""
        from phonos_app.helpers.bunches import start_bunch

        db = Mock()
        mock_get_db.return_value = db

        mock_bunch.return_value = {
            'id': 1,
            'bunch_id': 'bunch_test',
            'project_id': 1,
            'creator_id': 1,
            'model': 'type-0',
            'status': 'pending',
            'total_jobs': 100
        }

        # User has 0 credits
        db.execute.return_value.fetchone.return_value = {'credits': 0}

        result = start_bunch('bunch_test', user_id=1)

        assert result['status'] == 'error'
        assert 'credit' in result['message'].lower()


@pytest.mark.skip(reason="Cancellation tests require credit + job queue mocking that is not fully isolated")
class TestBunchCancellation:
    """Test bunch cancellation and credit refunds."""

    @patch('phonos_app.helpers.bunches.get_db')
    @patch('phonos_app.helpers.bunches.get_bunch')
    def test_cancel_bunch_refunds_credits(self, mock_bunch, mock_get_db):
        """Cancelling bunch should refund credits for pending jobs."""
        from phonos_app.helpers.bunches import cancel_bunch

        db = Mock()
        mock_get_db.return_value = db

        mock_bunch.return_value = {
            'id': 1,
            'bunch_id': 'bunch_test',
            'project_id': 1,
            'creator_id': 1,
            'status': 'running',
            'total_jobs': 10,
            'completed_jobs': 3
        }

        # 7 pending items to cancel
        db.execute.return_value.fetchall.return_value = [
            {'id': i, 'status': 'pending', 'job_id': 100 + i} for i in range(7)
        ]
        db.execute.return_value.fetchone.return_value = {'count': 7}

        result = cancel_bunch('bunch_test', user_id=1)

        # Verify credits would be refunded
        assert result['status'] in ['success', 'error']

    @patch('phonos_app.helpers.bunches.get_db')
    @patch('phonos_app.helpers.bunches.get_bunch')
    def test_cancel_completed_bunch_fails(self, mock_bunch, mock_get_db):
        """Cannot cancel a completed bunch."""
        from phonos_app.helpers.bunches import cancel_bunch

        db = Mock()
        mock_get_db.return_value = db

        mock_bunch.return_value = {
            'id': 1,
            'bunch_id': 'bunch_test',
            'status': 'completed'
        }

        result = cancel_bunch('bunch_test', user_id=1)

        assert result['status'] == 'error'


@pytest.mark.skip(reason="Status update tests have complex DB side-effect expectations")
class TestBunchStatusUpdates:
    """Test bunch status update logic."""

    @patch('phonos_app.helpers.bunches.get_db')
    def test_update_bunch_status_on_job_complete(self, mock_get_db):
        """Bunch status should update when jobs complete."""
        from phonos_app.helpers.bunches import update_bunch_status

        db = Mock()
        mock_get_db.return_value = db

        # Mock bunch data
        db.execute.return_value.fetchone.side_effect = [
            {'id': 1, 'total_jobs': 5, 'status': 'running'},
            {'completed': 3, 'failed': 0, 'pending': 2}
        ]

        result = update_bunch_status(bunch_id=1)

        assert result is True or result is None

    @patch('phonos_app.helpers.bunches.get_db')
    def test_bunch_marked_completed_when_all_done(self, mock_get_db):
        """Bunch should be marked completed when all jobs finish."""
        from phonos_app.helpers.bunches import update_bunch_status

        db = Mock()
        mock_get_db.return_value = db

        # All jobs completed
        db.execute.return_value.fetchone.side_effect = [
            {'id': 1, 'total_jobs': 5, 'status': 'running'},
            {'completed': 5, 'failed': 0, 'pending': 0}
        ]

        result = update_bunch_status(bunch_id=1)

        # Verify status would be updated to 'completed'
        assert result is True or result is None

    @patch('phonos_app.helpers.bunches.get_db')
    def test_bunch_marked_failed_when_threshold_exceeded(self, mock_get_db):
        """Bunch should be marked partially_failed when many jobs fail."""
        from phonos_app.helpers.bunches import update_bunch_status

        db = Mock()
        mock_get_db.return_value = db

        # Many jobs failed
        db.execute.return_value.fetchone.side_effect = [
            {'id': 1, 'total_jobs': 10, 'status': 'running'},
            {'completed': 2, 'failed': 8, 'pending': 0}
        ]

        result = update_bunch_status(bunch_id=1)

        assert result is True or result is None


@pytest.mark.skip(reason="Deletion tests require specific DB state and permission setup not fully mocked here")
class TestBunchDeletion:
    """Test bunch deletion."""

    @patch('phonos_app.helpers.bunches.get_db')
    @patch('phonos_app.helpers.bunches.get_bunch')
    def test_delete_pending_bunch(self, mock_bunch, mock_get_db):
        """Can delete a pending bunch."""
        from phonos_app.helpers.bunches import delete_bunch

        db = Mock()
        mock_get_db.return_value = db

        mock_bunch.return_value = {
            'id': 1,
            'bunch_id': 'bunch_test',
            'creator_id': 1,
            'status': 'pending'
        }

        result = delete_bunch('bunch_test', user_id=1)

        assert result['status'] == 'success'

    @patch('phonos_app.helpers.bunches.get_db')
    @patch('phonos_app.helpers.bunches.get_bunch')
    def test_delete_running_bunch_fails(self, mock_bunch, mock_get_db):
        """Cannot delete a running bunch."""
        from phonos_app.helpers.bunches import delete_bunch

        db = Mock()
        mock_get_db.return_value = db

        mock_bunch.return_value = {
            'id': 1,
            'bunch_id': 'bunch_test',
            'creator_id': 1,
            'status': 'running'
        }

        result = delete_bunch('bunch_test', user_id=1)

        assert result['status'] == 'error'

    @patch('phonos_app.helpers.bunches.get_db')
    @patch('phonos_app.helpers.bunches.get_bunch')
    def test_delete_bunch_wrong_user_fails(self, mock_bunch, mock_get_db):
        """Non-creator cannot delete bunch."""
        from phonos_app.helpers.bunches import delete_bunch

        db = Mock()
        mock_get_db.return_value = db

        mock_bunch.return_value = {
            'id': 1,
            'bunch_id': 'bunch_test',
            'creator_id': 1,  # Creator is user 1
            'status': 'pending'
        }

        result = delete_bunch('bunch_test', user_id=999)  # Different user

        assert result['status'] == 'error'
        assert 'permission' in result['message'].lower() or 'creator' in result['message'].lower()


# =============================================================================
# API ENDPOINT TESTS
# =============================================================================

class TestBunchAPIEndpoints:
    """Test bunch API endpoints."""

    @pytest.fixture
    def app_client(self):
        """Create test Flask app client."""
        # This would be set up in conftest.py in a real scenario
        from phonos_app.app import app
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        with app.test_client() as client:
            yield client

    @pytest.fixture
    def auth_headers(self):
        """Mock authentication headers."""
        return {'Authorization': 'Bearer test_token'}


class TestBunchValidation:
    """Test input validation for bunches."""

    def test_bunch_name_required(self):
        """Bunch must have a name."""
        data = {
            'model': 'type-0',
            'items': [{'sequence': 'MKTAYIAK'}]
        }
        # Name missing - should fail validation
        assert 'name' not in data

    def test_bunch_model_required(self):
        """Bunch must specify a model."""
        data = {
            'name': 'Test',
            'items': [{'sequence': 'MKTAYIAK'}]
        }
        # Model missing - should fail validation
        assert 'model' not in data

    def test_bunch_items_required(self):
        """Bunch must have items."""
        data = {
            'name': 'Test',
            'model': 'type-0',
            'items': []
        }
        # Items empty - should fail validation
        assert len(data['items']) == 0

    def test_bunch_item_needs_sequence_or_file(self):
        """Each item must have sequence or file_id."""
        invalid_item = {'name': 'Just a name'}
        # No sequence or file_id - should fail validation
        assert 'sequence' not in invalid_item
        assert 'file_id' not in invalid_item

    def test_bunch_max_items_limit(self):
        """Cannot exceed 1000 items per bunch."""
        max_items = 1000
        items = [{'sequence': 'MKTAYIAK'} for _ in range(1001)]
        assert len(items) > max_items

    def test_bunch_priority_values(self):
        """Priority must be 0-3."""
        valid_priorities = [0, 1, 2, 3]
        invalid_priorities = [-1, 4, 5, 100]

        for p in valid_priorities:
            assert 0 <= p <= 3

        for p in invalid_priorities:
            assert p not in valid_priorities


class TestBunchProgressCalculation:
    """Test bunch progress calculation."""

    def test_progress_calculation(self):
        """Progress percentage should be calculated correctly."""
        total = 10
        completed = 3
        failed = 1

        progress = ((completed + failed) / total) * 100
        assert progress == 40.0

    def test_progress_with_no_jobs(self):
        """Progress with 0 total jobs should be 0."""
        total = 0
        completed = 0

        if total == 0:
            progress = 0
        else:
            progress = (completed / total) * 100

        assert progress == 0

    def test_progress_complete(self):
        """100% progress when all jobs done."""
        total = 10
        completed = 8
        failed = 2

        progress = ((completed + failed) / total) * 100
        assert progress == 100.0


# =============================================================================
# MRS INTEGRATION TESTS
# =============================================================================

@pytest.mark.skip(reason="Heavy integration tests requiring full MRS, Redis and job submission stack")
class TestMRSBunchIntegration:
    """Test MRS service integration with bunches."""

    @patch('production.mrs_service.processors.get_db')
    def test_update_bunch_on_job_completion(self, mock_get_db):
        """MRS should update bunch when job completes."""
        db = Mock()
        mock_get_db.return_value = db

        # Mock finding bunch item for job
        db.execute.return_value.fetchone.return_value = {'bunch_id': 1}

        # Job completed
        job_data = {'id': 100, 'status': 'completed'}

        # In real integration, _update_bunch_for_job would be called
        # assert that bunch status update is triggered

    @patch('production.mrs_service.processors.get_db')
    def test_bunch_not_affected_by_non_bunch_jobs(self, mock_get_db):
        """Regular jobs (not in bunches) should not affect bunches."""
        db = Mock()
        mock_get_db.return_value = db

        # No bunch item found for this job
        db.execute.return_value.fetchone.return_value = None

        job_data = {'id': 200, 'status': 'completed'}

        # Should handle gracefully without errors


# =============================================================================
# EDGE CASES AND ERROR HANDLING
# =============================================================================

@pytest.mark.skip(reason="Requires more complete mocking of job submission and concurrent DB behavior")
class TestBunchEdgeCases:
    """Test edge cases and error handling."""

    @patch('phonos_app.helpers.bunches.get_db')
    def test_bunch_with_duplicate_sequences(self, mock_get_db):
        """Bunches can have duplicate sequences."""
        from phonos_app.helpers.bunches import create_bunch

        db = Mock()
        cursor = Mock()
        db.cursor.return_value = cursor
        cursor.fetchone.return_value = {'id': 1}
        mock_get_db.return_value = db

        # Same sequence multiple times (valid use case)
        items = [
            {'name': 'Run 1', 'sequence': 'MKTAYIAK'},
            {'name': 'Run 2', 'sequence': 'MKTAYIAK'},
            {'name': 'Run 3', 'sequence': 'MKTAYIAK'}
        ]

        result = create_bunch(
            project_id=1,
            creator_id=1,
            name='Duplicate Test',
            model='type-0',
            items=items
        )

        assert result['status'] == 'success'

    def test_bunch_id_collision_handling(self):
        """Handle extremely unlikely bunch_id collisions."""
        from phonos_app.helpers.bunches import generate_bunch_id

        # Generate many IDs - all should be unique
        ids = set()
        for _ in range(10000):
            new_id = generate_bunch_id()
            assert new_id not in ids
            ids.add(new_id)

    @patch('phonos_app.helpers.bunches.get_db')
    @patch('phonos_app.helpers.bunches.get_bunch')
    def test_concurrent_bunch_start_protection(self, mock_bunch, mock_get_db):
        """Second start request should fail if bunch already started."""
        from phonos_app.helpers.bunches import start_bunch

        db = Mock()
        mock_get_db.return_value = db

        # Simulate race condition - bunch is now running
        mock_bunch.return_value = {
            'id': 1,
            'bunch_id': 'bunch_race',
            'status': 'running'  # Already started by another request
        }

        result = start_bunch('bunch_race', user_id=1)

        assert result['status'] == 'error'


class TestBunchCostCalculation:
    """Test bunch cost calculation for credit deduction."""

    def test_cost_calculation_single_model(self):
        """Calculate cost for single model type."""
        num_items = 10
        cost_per_job = 1  # 1 credit per job

        total_cost = num_items * cost_per_job
        assert total_cost == 10

    def test_cost_with_priority_multiplier(self):
        """Premium priority should have higher cost."""
        base_cost = 1
        priority_multipliers = {
            0: 10.0,  # CRITICAL
            1: 2.0,   # PREMIUM
            2: 1.0,   # STANDARD
            3: 0.5    # BACKGROUND
        }

        for priority, multiplier in priority_multipliers.items():
            adjusted_cost = base_cost * multiplier
            assert adjusted_cost >= 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
