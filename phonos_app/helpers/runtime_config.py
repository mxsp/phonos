"""
Runtime Configuration Manager

Manages runtime levels (machine tiers) for notebook kernels.
Stores backend connection configs encrypted with AES-256-GCM
via existing user_model_crypto utilities.
"""

import os
import json
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# Load validation schema once
_SCHEMA = None

def _get_schema():
    global _SCHEMA
    if _SCHEMA is None:
        schema_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            'config', 'runtimes.schema.json'
        )
        try:
            with open(schema_path, 'r') as f:
                _SCHEMA = json.load(f)
        except FileNotFoundError:
            logger.warning(f"Runtime schema not found at {schema_path}")
            _SCHEMA = {"definitions": {}}
    return _SCHEMA


def _get_master_key():
    """Get master encryption key from environment."""
    key = os.environ.get('ENCRYPTION_KEY')
    if not key:
        logger.warning("ENCRYPTION_KEY is not set; runtime config encryption is disabled.")
        return None
    return key


class RuntimeConfig:
    """
    CRUD operations for runtime levels with encrypted config storage.
    """

    VALID_BACKEND_TYPES = ('local_docker', 'remote_docker', 'ssh', 'kubernetes', 'remote_ssh', 'wolfram_service')

    def __init__(self, db=None):
        self._db = db

    @property
    def db(self):
        if self._db is not None:
            return self._db
        # Always resolve request-scoped DB connections on demand.
        from .database import get_db
        return get_db()

    @staticmethod
    def _default_basic_level() -> Dict[str, Any]:
        return {
            'name': 'basic',
            'display_name': 'Basic',
            'backend_type': 'local_docker',
            'mem_limit': '2g',
            'cpu_quota': 1.0,
            'gpu_enabled': False,
            'max_sessions': 5,
            'sort_order': 0,
            'is_active': True,
            'config': {},
        }

    @staticmethod
    def _default_wolfram_level() -> Dict[str, Any]:
        import os as _os
        return {
            'name': 'wolfram',
            'display_name': 'Wolfram Language',
            'backend_type': 'wolfram_service',
            'mem_limit': '1g',
            'cpu_quota': 0.5,
            'gpu_enabled': False,
            'max_sessions': 3,
            'sort_order': 10,
            'is_active': True,
            'config': {
                'service_url': _os.environ.get('WOLFRAM_SERVICE_URL', 'http://localhost:5100'),
                'kernel': _os.environ.get('WOLFRAM_KERNEL', 'wolframscript'),
                'use_cloud': True,
                'timeout': 60,
            },
        }

    @staticmethod
    def _default_quantum_level() -> Dict[str, Any]:
        return {
            'name': 'quantum',
            'display_name': 'Python 3.11 Quantum',
            'backend_type': 'local_docker',
            'mem_limit': '2g',
            'cpu_quota': 1.0,
            'gpu_enabled': False,
            'max_sessions': 3,
            'sort_order': 5,
            'is_active': True,
            'config': {},
        }

    @staticmethod
    def _default_hybrid_level() -> Dict[str, Any]:
        # Experimental combined runtime: Python quantum kernel container for
        # code cells + the Wolfram service for wolfram cells, bridged via the
        # in-kernel ``phonos.wolfram`` helper (shared Wolfram session state).
        return {
            'name': 'hybrid',
            'display_name': 'Quantum + Wolfram (Experimental)',
            'backend_type': 'local_docker',
            'mem_limit': '2g',
            'cpu_quota': 1.0,
            'gpu_enabled': False,
            'max_sessions': 2,
            'sort_order': 15,
            'is_active': True,
            'config': {},
        }

    @staticmethod
    def _is_missing_runtime_levels_table(error: Exception) -> bool:
        message = str(error).lower()
        return (
            'runtime_levels' in message and
            ('does not exist' in message or 'undefinedtable' in message or 'no such table' in message)
        )

    def _safe_rollback(self):
        try:
            self.db.rollback()
        except Exception:
            pass

    def get_levels(self, include_inactive=False) -> List[Dict[str, Any]]:
        """Query all runtime levels, decrypting configs."""
        try:
            if include_inactive:
                rows = self.db.execute(
                    'SELECT * FROM runtime_levels ORDER BY sort_order, name'
                ).fetchall()
            else:
                rows = self.db.execute(
                    'SELECT * FROM runtime_levels WHERE is_active = 1 ORDER BY sort_order, name'
                ).fetchall()
        except Exception as e:
            if self._is_missing_runtime_levels_table(e):
                logger.warning("runtime_levels table is missing; using built-in runtime levels")
                self._safe_rollback()
                return [
                    self._default_basic_level(),
                    self._default_quantum_level(),
                    self._default_wolfram_level(),
                    self._default_hybrid_level(),
                ]
            self._safe_rollback()
            raise

        levels = []
        names = set()
        for row in rows:
            level = dict(row)
            level['config'] = self._decrypt_config(level.pop('config_encrypted', None))
            levels.append(level)
            names.add(level['name'])

        # Synthesize built-in levels that aren't configured in the DB. The
        # ``wolfram_service`` backend is virtual (no Docker container) and the
        # quantum/hybrid levels reuse the standard kernel image, so exposing
        # them is always safe.
        if 'wolfram' not in names:
            levels.append(self._default_wolfram_level())
        if 'quantum' not in names:
            levels.append(self._default_quantum_level())
        if 'hybrid' not in names:
            levels.append(self._default_hybrid_level())

        levels.sort(key=lambda l: (l.get('sort_order', 0), l.get('name', '')))
        return levels

    def get_level(self, name: str) -> Optional[Dict[str, Any]]:
        """Get a single runtime level with decrypted config."""
        try:
            row = self.db.execute(
                'SELECT * FROM runtime_levels WHERE name = %s', (name,)
            ).fetchone()
        except Exception as e:
            if self._is_missing_runtime_levels_table(e):
                self._safe_rollback()
                builtin = {
                    'basic': self._default_basic_level,
                    'wolfram': self._default_wolfram_level,
                    'quantum': self._default_quantum_level,
                    'hybrid': self._default_hybrid_level,
                }.get(name)
                return builtin() if builtin else None
            self._safe_rollback()
            raise

        if not row:
            if name == 'basic':
                return self._default_basic_level()
            if name == 'wolfram':
                return self._default_wolfram_level()
            if name == 'quantum':
                return self._default_quantum_level()
            if name == 'hybrid':
                return self._default_hybrid_level()
            return None

        level = dict(row)
        level['config'] = self._decrypt_config(level.pop('config_encrypted', None))
        return level

    def create_level(
        self,
        name: str,
        display_name: str,
        backend_type: str,
        config: Optional[Dict[str, Any]] = None,
        mem_limit: str = '2g',
        cpu_quota: float = 1.0,
        gpu_enabled: bool = False,
        max_sessions: int = 5,
        sort_order: int = 0
    ) -> Dict[str, Any]:
        """Create a new runtime level with encrypted config."""
        if backend_type not in self.VALID_BACKEND_TYPES:
            raise ValueError(f"Invalid backend_type: {backend_type}")

        self.validate_level_config(backend_type, config)

        encrypted = self._encrypt_config(config) if config else None

        row = self.db.execute('''
            INSERT INTO runtime_levels
            (name, display_name, backend_type, config_encrypted, mem_limit, cpu_quota,
             gpu_enabled, max_sessions, sort_order, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
            RETURNING *
        ''', (name, display_name, backend_type, encrypted, mem_limit, cpu_quota,
              int(gpu_enabled) if gpu_enabled is not None else 0,
              max_sessions, sort_order)).fetchone()
        self.db.commit()

        result = dict(row)
        result['config'] = config
        result.pop('config_encrypted', None)
        return result

    def update_level(self, name: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Update an existing runtime level."""
        existing = self.get_level(name)
        if not existing:
            return None

        updates = []
        params = []

        for field in ('display_name', 'backend_type', 'mem_limit', 'cpu_quota',
                       'max_sessions', 'sort_order'):
            if field in kwargs:
                updates.append(f'{field} = %s')
                params.append(kwargs[field])

        for field in ('gpu_enabled', 'is_active'):
            if field in kwargs:
                updates.append(f'{field} = %s')
                value = kwargs[field]
                if isinstance(value, bool):
                    value = 1 if value else 0
                params.append(value)

        if 'config' in kwargs:
            bt = kwargs.get('backend_type', existing['backend_type'])
            self.validate_level_config(bt, kwargs['config'])
            updates.append('config_encrypted = %s')
            params.append(self._encrypt_config(kwargs['config']) if kwargs['config'] else None)

        if not updates:
            return existing

        params.append(name)
        self.db.execute(f'''
            UPDATE runtime_levels SET {', '.join(updates)} WHERE name = %s
        ''', params)
        self.db.commit()

        return self.get_level(name)

    def delete_level(self, name: str) -> bool:
        """Soft-delete a runtime level by setting is_active=False."""
        if name == 'basic':
            raise ValueError("Cannot delete the default 'basic' runtime level")

        result = self.db.execute(
            'UPDATE runtime_levels SET is_active = 0 WHERE name = %s AND is_active = 1',
            (name,)
        )
        self.db.commit()
        return result.rowcount > 0

    def validate_level_config(self, backend_type: str, config: Optional[Dict[str, Any]]):
        """Validate config against the JSON schema for the backend type."""
        if backend_type not in self.VALID_BACKEND_TYPES:
            raise ValueError(f"Invalid backend_type: {backend_type}")

        # local_docker requires no config
        if backend_type == 'local_docker':
            return

        if not config:
            if backend_type != 'local_docker':
                raise ValueError(f"Config is required for backend_type '{backend_type}'")
            return

        schema = _get_schema()
        definition = schema.get('definitions', {}).get(backend_type, {})

        # Check required fields
        required = definition.get('required', [])
        for field in required:
            if field not in config:
                raise ValueError(f"Missing required config field '{field}' for {backend_type}")

        # Check allowed fields
        allowed_props = set(definition.get('properties', {}).keys())
        if allowed_props:
            extra = set(config.keys()) - allowed_props
            if extra:
                raise ValueError(f"Unknown config fields for {backend_type}: {extra}")

    def _encrypt_config(self, config: Dict[str, Any]) -> str:
        """Encrypt config dict using master key."""
        key = _get_master_key()
        if not key:
            raise EnvironmentError("ENCRYPTION_KEY is not set; cannot encrypt runtime config.")
        from .user_model_crypto import encrypt_data
        return encrypt_data(config, key)

    def _decrypt_config(self, encrypted: Optional[str]) -> Optional[Dict[str, Any]]:
        """Decrypt config, returning None if no encrypted data."""
        if not encrypted:
            return None
        try:
            from .user_model_crypto import decrypt_data
            result = decrypt_data(encrypted, _get_master_key())
            if isinstance(result, dict):
                return result
            if isinstance(result, bytes):
                return json.loads(result.decode('utf-8'))
            return None
        except Exception as e:
            logger.error(f"Failed to decrypt runtime config: {e}")
            return None


# Convenience singleton
_instance = None

def get_runtime_config(db=None) -> RuntimeConfig:
    """Get or create a RuntimeConfig instance."""
    global _instance
    # Never store request-bound DB handles in the singleton; they can become
    # stale/closed after request teardown and poison future calls.
    if db is not None:
        return RuntimeConfig(db)
    if _instance is None:
        _instance = RuntimeConfig()
    return _instance
