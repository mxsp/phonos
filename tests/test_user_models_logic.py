
import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import json
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'production')))

# Mock modules that might not be available or need mocking
sys.modules['phonos_app.helpers.database'] = MagicMock()
sys.modules['phonos_app.helpers.api_keys'] = MagicMock()

# Now import the module to test
from phonos_app.helpers.user_models import (
    create_user_model,
    get_user_models,
    update_user_model,
    create_model_api_key,
    verify_model_api_key,
    get_user_model
)

class TestUserModelsLogic(unittest.TestCase):
    def setUp(self):
        # Setup the mock database
        self.mock_db = MagicMock()
        self.mock_get_db_patcher = patch('phonos_app.helpers.user_models.get_db', return_value=self.mock_db)
        self.mock_get_db = self.mock_get_db_patcher.start()

        # Mock generate_api_key
        self.mock_generate_api_key_patcher = patch('phonos_app.helpers.user_models.generate_api_key')
        self.mock_generate_api_key = self.mock_generate_api_key_patcher.start()
        self.mock_generate_api_key.return_value = ('pk_123', 'hash_123', 'pk_123...', 'key_id_123')
        
        # Mock secrets and encryption
        self.mock_secrets_patcher = patch('phonos_app.helpers.user_models.secrets')
        self.mock_secrets = self.mock_secrets_patcher.start()
        self.mock_secrets.choice.side_effect = lambda seq: seq[0] # Deterministic random

    def tearDown(self):
        self.mock_get_db_patcher.stop()
        self.mock_generate_api_key_patcher.stop()
        self.mock_secrets_patcher.stop()

    def test_create_user_model(self):
        # Setup
        user_id = 1
        model_name = "Test Model"
        model_description = "A test model"
        input_schema = [{"name": "prompt", "type": "string"}]
        
        # Mock db response
        mock_row = {
            'id': 101,
            'model_id': 'unique_id_123',
            'created_at': datetime(2023, 1, 1, 12, 0, 0)
        }
        self.mock_db.execute.return_value.fetchone.return_value = mock_row

        # Execute
        result = create_user_model(
            user_id=user_id,
            model_name=model_name,
            model_description=model_description,
            input_schema=input_schema
        )

        # Verify
        self.assertEqual(result['id'], 101)
        self.assertEqual(result['model_name'], model_name)
        
        # Verify DB call
        args, _ = self.mock_db.execute.call_args
        sql = args[0]
        params = args[1]
        
        self.assertIn("INSERT INTO user_models", sql)
        self.assertEqual(params[1], user_id)
        self.assertEqual(params[3], model_name)
        self.assertEqual(params[6], json.dumps(input_schema))

    def test_get_user_models(self):
        # Setup
        user_id = 1
        
        # Mock db response
        mock_rows = [
            {
                'id': 101,
                'model_id': 'model_1',
                'user_id': 1,
                'project_id': None,
                'project_name': None,
                'model_name': 'Model 1',
                'model_description': 'Desc 1',
                'model_type': 'custom',
                'input_schema': '[{"name": "p"}]',
                'output_format': 'json',
                'outline_color': 'gray',
                'badge': None,
                'badge_color': None,
                'credit_cost': 10,
                'estimated_runtime_seconds': 60,
                'is_active': True,
                'is_public': False,
                'created_at': datetime(2023, 1, 1),
                'last_used_at': None,
                'total_jobs_run': 0,
                'owner_username': 'user1'
            }
        ]
        self.mock_db.execute.return_value.fetchall.return_value = mock_rows

        # Execute
        results = get_user_models(user_id)

        # Verify
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['id'], 101)
        self.assertEqual(results[0]['model_name'], 'Model 1')
        self.assertEqual(results[0]['is_owned_by_user'], True)

    def test_update_user_model(self):
        # Setup
        model_id = 101
        user_id = 1
        updates = {'model_name': 'Updated Name', 'credit_cost': 20}
        
        # Mock db response
        self.mock_db.execute.return_value.fetchone.return_value = {'id': 101}

        # Execute
        success = update_user_model(model_id, user_id, **updates)

        # Verify
        self.assertTrue(success)
        
        # Verify DB call
        args, _ = self.mock_db.execute.call_args
        sql = args[0]
        params = args[1]
        
        self.assertIn("UPDATE user_models", sql)
        self.assertIn("model_name = %s", sql)
        self.assertIn("credit_cost = %s", sql)
        # Check params contains values
        self.assertIn('Updated Name', params)
        self.assertIn(20, params)

    def test_update_user_model_validation(self):
        # Setup
        model_id = 101
        user_id = 1
        
        # Test invalid schema
        with self.assertRaises(ValueError):
            update_user_model(model_id, user_id, input_schema="not a list")
            
        # Test empty name
        with self.assertRaises(ValueError):
            update_user_model(model_id, user_id, model_name="  ")

    def test_create_model_api_key(self):
        # Setup
        user_id = 1
        name = "Test Key"
        user_model_id = 101
        
        # Mock db response
        mock_row = {
            'id': 501,
            'created_at': datetime(2023, 1, 1)
        }
        self.mock_db.execute.return_value.fetchone.return_value = mock_row

        # Execute
        result = create_model_api_key(
            user_id=user_id,
            name=name,
            user_model_id=user_model_id
        )

        # Verify
        self.assertEqual(result['key_id'], 501)
        self.assertEqual(result['full_key'], 'pk_123')
        
        # Verify DB call
        args, _ = self.mock_db.execute.call_args
        sql = args[0]
        params = args[1]
        
        self.assertIn("INSERT INTO user_model_api_keys", sql)
        self.assertEqual(params[0], user_model_id) # user_model_id
        self.assertEqual(params[2], user_id) # user_id
        self.assertEqual(params[3], 'key_id_123') # key_id
        self.assertEqual(params[4], 'hash_123') # key_hash

    def test_verify_model_api_key(self):
        # Setup
        api_key = "pk_test_key"
        
        # Mock db response for lookup
        mock_row = {
            'id': 501,
            'user_model_id': 101,
            'project_id': None,
            'user_id': 1,
            'rate_limit_per_minute': 60
        }
        self.mock_db.execute.return_value.fetchone.return_value = mock_row

        # Execute
        result = verify_model_api_key(api_key)

        # Verify
        self.assertIsNotNone(result)
        self.assertEqual(result['key_id'], 501)
        self.assertEqual(result['user_model_id'], 101)
        
        # Verify update call (increment usage)
        # Should be the second call to execute (first was select)
        self.assertEqual(self.mock_db.execute.call_count, 2)
        args, _ = self.mock_db.execute.call_args
        sql = args[0]
        self.assertIn("UPDATE user_model_api_keys", sql)

if __name__ == '__main__':
    unittest.main()
