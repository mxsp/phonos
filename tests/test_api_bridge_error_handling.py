
import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import json

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'production')))

# Mock redis before importing app
sys.modules['redis'] = MagicMock()
sys.modules['redis.Redis'] = MagicMock()
sys.modules['redis.ConnectionPool'] = MagicMock()

from phonos_app.app import app

class TestApiBridgeErrorHandling(unittest.TestCase):
    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

    @patch('phonos_app.routes.api_bridge.authenticate_bridge_request')
    def test_bridge_poll_auth_error(self, mock_auth):
        # Simulate an exception during authentication
        mock_auth.side_effect = Exception("Database connection failed")

        response = self.app.post('/api/bridge/v1/jobs/poll', 
                               headers={'Authorization': 'Bearer test_key'},
                               json={'max_jobs': 1})

        self.assertEqual(response.status_code, 500)
        data = json.loads(response.data)
        self.assertEqual(data['status'], 'error')
        self.assertEqual(data['message'], 'Authentication failed due to internal error')

    @patch('phonos_app.routes.api_bridge.authenticate_bridge_request')
    def test_bridge_submit_result_auth_error(self, mock_auth):
        # Simulate an exception during authentication
        mock_auth.side_effect = Exception("Database connection failed")

        response = self.app.post('/api/bridge/v1/jobs/job_123/result', 
                               headers={'Authorization': 'Bearer test_key'},
                               json={'status': 'completed'})

        self.assertEqual(response.status_code, 500)
        data = json.loads(response.data)
        self.assertEqual(data['status'], 'error')
        self.assertEqual(data['message'], 'Authentication failed due to internal error')

    @patch('phonos_app.routes.api_bridge.authenticate_bridge_request')
    def test_bridge_worker_info_auth_error(self, mock_auth):
        # Simulate an exception during authentication
        mock_auth.side_effect = Exception("Database connection failed")

        response = self.app.get('/api/bridge/v1/worker/info', 
                              headers={'Authorization': 'Bearer test_key'})

        self.assertEqual(response.status_code, 500)
        data = json.loads(response.data)
        self.assertEqual(data['status'], 'error')
        self.assertEqual(data['message'], 'Authentication failed due to internal error')

if __name__ == '__main__':
    unittest.main()
