"""
Tests for User Model Security Features

Tests:
- Output data sanitization (sensitive field removal)
- Format-specific field filtering
- API-key-based encryption/decryption
- Input data sanitization
"""

import json
import pytest
from phonos_app.helpers.user_model_security import (
    sanitize_output_data,
    sanitize_job_input_data,
    derive_encryption_key_from_api_key,
    encrypt_with_api_key,
    decrypt_with_api_key
)


class TestOutputDataSanitization:
    """Test sanitize_output_data function."""

    def test_removes_sensitive_fields(self):
        """Should remove fields containing sensitive keywords."""
        data = {
            'structure_pdb': 'ATOM...',
            'password': 'secret123',
            'api_key': 'pk_abc',
            'user_id': 42,
            'confidence_scores': [0.95, 0.92]
        }

        sanitized = sanitize_output_data(data, 'pdb')

        assert 'structure_pdb' in sanitized
        assert 'confidence_scores' in sanitized
        assert 'password' not in sanitized
        assert 'api_key' not in sanitized
        assert 'user_id' not in sanitized

    def test_filters_by_format_pdb(self):
        """Should only keep PDB-relevant fields for pdb format."""
        data = {
            'structure_pdb': 'ATOM...',
            'confidence_scores': {'plddt': [0.95]},
            'image_data': 'iVBORw0K...',  # Not PDB-relevant
            'random_field': 'value'  # Not PDB-relevant
        }

        sanitized = sanitize_output_data(data, 'pdb')

        assert 'structure_pdb' in sanitized
        assert 'confidence_scores' in sanitized
        # Non-PDB fields should be filtered out
        assert 'image_data' not in sanitized
        assert 'random_field' not in sanitized

    def test_filters_by_format_png(self):
        """Should only keep image-relevant fields for png format."""
        data = {
            'image_base64': 'iVBORw0K...',
            'width': 1920,
            'height': 1080,
            'structure_pdb': 'ATOM...',  # Not image-relevant
            'metrics': {'runtime': 4.2}  # Metadata - should be kept
        }

        sanitized = sanitize_output_data(data, 'png')

        assert 'image_base64' in sanitized
        assert 'width' in sanitized
        assert 'height' in sanitized
        assert 'metrics' in sanitized  # Metadata allowed
        assert 'structure_pdb' not in sanitized

    def test_allows_all_fields_for_json_format(self):
        """JSON format should allow all non-sensitive fields."""
        data = {
            'prediction': 'positive',
            'confidence': 0.95,
            'metadata': {'version': '1.0'},
            'api_key': 'pk_secret'  # Still removed (sensitive)
        }

        sanitized = sanitize_output_data(data, 'json')

        assert 'prediction' in sanitized
        assert 'confidence' in sanitized
        assert 'metadata' in sanitized
        assert 'api_key' not in sanitized

    def test_handles_nested_dicts(self):
        """Should recursively sanitize nested dictionaries."""
        data = {
            'result': {
                'structure_pdb': 'ATOM...',
                'metadata': {
                    'user_id': 42,  # Sensitive - nested
                    'version': '1.0'
                }
            }
        }

        sanitized = sanitize_output_data(data, 'pdb')

        assert 'result' in sanitized
        assert 'structure_pdb' in sanitized['result']
        assert 'metadata' in sanitized['result']
        assert 'user_id' not in sanitized['result']['metadata']
        assert 'version' in sanitized['result']['metadata']

    def test_handles_lists(self):
        """Should recursively sanitize lists of dictionaries."""
        data = {
            'models': [
                {'structure_pdb': 'ATOM 1', 'password': 'secret1'},
                {'structure_pdb': 'ATOM 2', 'api_key': 'pk_123'}
            ]
        }

        sanitized = sanitize_output_data(data, 'pdb')

        assert 'models' in sanitized
        assert len(sanitized['models']) == 2
        assert 'structure_pdb' in sanitized['models'][0]
        assert 'password' not in sanitized['models'][0]
        assert 'api_key' not in sanitized['models'][1]


class TestInputDataSanitization:
    """Test sanitize_job_input_data function."""

    def test_removes_sensitive_fields_from_input(self):
        """Should remove sensitive fields from job input data."""
        data = {
            'sequence': 'MKLAVLAA...',
            'user_id': 42,
            'creator_id': 13,
            'internal_id': 999
        }

        sanitized = sanitize_job_input_data(data)

        assert 'sequence' in sanitized
        assert 'user_id' not in sanitized
        assert 'creator_id' not in sanitized
        assert 'internal_id' not in sanitized

    def test_handles_nested_input_data(self):
        """Should recursively sanitize nested input data."""
        data = {
            'params': {
                'temperature': 0.7,
                'api_key': 'sk_secret'
            }
        }

        sanitized = sanitize_job_input_data(data)

        assert 'params' in sanitized
        assert 'temperature' in sanitized['params']
        assert 'api_key' not in sanitized['params']


class TestAPIKeyEncryption:
    """Test API-key-based encryption/decryption."""

    def test_derive_key_is_deterministic(self):
        """Same API key should always derive the same encryption key."""
        api_key = "pk_test_abc123"

        key1 = derive_encryption_key_from_api_key(api_key)
        key2 = derive_encryption_key_from_api_key(api_key)

        assert key1 == key2

    def test_different_api_keys_produce_different_keys(self):
        """Different API keys should produce different encryption keys."""
        key1 = derive_encryption_key_from_api_key("pk_test_abc123")
        key2 = derive_encryption_key_from_api_key("pk_test_xyz789")

        assert key1 != key2

    def test_encrypt_decrypt_round_trip(self):
        """Should be able to encrypt and decrypt data."""
        api_key = "pk_test_abc123"
        plaintext = b"Hello, World!"

        encrypted = encrypt_with_api_key(plaintext, api_key)
        assert encrypted is not None
        assert encrypted != plaintext

        decrypted = decrypt_with_api_key(encrypted, api_key)
        assert decrypted == plaintext

    def test_encrypt_decrypt_json_data(self):
        """Should handle JSON data encryption."""
        api_key = "pk_test_abc123"
        data = {'status': 'completed', 'result': {'prediction': 'positive'}}
        plaintext = json.dumps(data).encode('utf-8')

        encrypted = encrypt_with_api_key(plaintext, api_key)
        decrypted = decrypt_with_api_key(encrypted, api_key)

        assert decrypted == plaintext
        assert json.loads(decrypted.decode('utf-8')) == data

    def test_wrong_key_fails_decryption(self):
        """Decryption with wrong API key should fail."""
        plaintext = b"Sensitive data"

        encrypted = encrypt_with_api_key(plaintext, "pk_test_correct")
        decrypted = decrypt_with_api_key(encrypted, "pk_test_wrong")

        assert decrypted is None  # Decryption should fail

    def test_handles_large_data(self):
        """Should handle large data encryption."""
        api_key = "pk_test_abc123"
        # Create a large JSON payload (~1MB)
        large_data = json.dumps({
            'structure_pdb': 'ATOM' * 10000,
            'confidence': [0.95] * 5000
        }).encode('utf-8')

        encrypted = encrypt_with_api_key(large_data, api_key)
        assert encrypted is not None

        decrypted = decrypt_with_api_key(encrypted, api_key)
        assert decrypted == large_data


class TestIntegration:
    """Integration tests combining sanitization and encryption."""

    def test_full_workflow_output(self):
        """Test full workflow: sanitize -> encrypt -> decrypt."""
        api_key = "pk_test_abc123"

        # Raw result from worker (contains sensitive data)
        raw_result = {
            'structure_pdb': 'ATOM 1...',
            'confidence_scores': {'plddt': [0.95, 0.92]},
            'user_id': 42,  # Sensitive
            'api_key': 'pk_secret'  # Sensitive
        }

        # Sanitize
        sanitized = sanitize_output_data(raw_result, 'pdb')
        assert 'user_id' not in sanitized
        assert 'api_key' not in sanitized

        # Encrypt
        result_json = json.dumps(sanitized).encode('utf-8')
        encrypted = encrypt_with_api_key(result_json, api_key)
        assert encrypted is not None

        # Decrypt
        decrypted = decrypt_with_api_key(encrypted, api_key)
        restored = json.loads(decrypted.decode('utf-8'))

        assert restored == sanitized
        assert 'structure_pdb' in restored
        assert 'confidence_scores' in restored
        assert 'user_id' not in restored

    def test_full_workflow_input(self):
        """Test input sanitization before encryption."""
        api_key = "pk_test_abc123"

        # Raw input from job (may contain sensitive data)
        raw_input = {
            'sequence': 'MKLAVLAA...',
            'creator_id': 13,  # Sensitive
            'system_path': '/var/data'  # Sensitive
        }

        # Sanitize
        sanitized = sanitize_job_input_data(raw_input)
        assert 'creator_id' not in sanitized
        assert 'system_path' not in sanitized

        # Encrypt
        input_json = json.dumps(sanitized).encode('utf-8')
        encrypted = encrypt_with_api_key(input_json, api_key)
        assert encrypted is not None

        # Worker decrypts
        decrypted = decrypt_with_api_key(encrypted, api_key)
        restored = json.loads(decrypted.decode('utf-8'))

        assert restored == sanitized
        assert 'sequence' in restored
        assert 'creator_id' not in restored


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
