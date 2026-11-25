#!/usr/bin/env python3
"""Transform api_keys_management.py to use register function pattern."""

import re

source_file = '/opt/phonos/phonos_app/routes/api_keys_management.py'

# Read the original file
with open(source_file, 'r') as f:
    content = f.read()

# New header with imports
new_header = '''"""API key management endpoints - available for all users with credit-based usage."""

import logging
import importlib.util
import os
from flask import Flask, jsonify, request, session

# Import helpers to avoid circular imports
_base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_helpers_path = os.path.join(_base_dir, 'helpers')

# Import api_keys helper
_api_keys_spec = importlib.util.spec_from_file_location('api_keys', os.path.join(_helpers_path, 'api_keys.py'))
_api_keys_module = importlib.util.module_from_spec(_api_keys_spec)
_api_keys_spec.loader.exec_module(_api_keys_module)

create_api_key = _api_keys_module.create_api_key
get_user_api_keys = _api_keys_module.get_user_api_keys
delete_api_key = _api_keys_module.delete_api_key
toggle_api_key = _api_keys_module.toggle_api_key
create_worker_api_key = _api_keys_module.create_worker_api_key
get_user_worker_keys = _api_keys_module.get_user_worker_keys

# Import auth helper
_auth_spec = importlib.util.spec_from_file_location('auth', os.path.join(_helpers_path, 'auth.py'))
_auth_module = importlib.util.module_from_spec(_auth_spec)
_auth_spec.loader.exec_module(_auth_module)

login_required = _auth_module.login_required

# Import security helper
_security_spec = importlib.util.spec_from_file_location('security', os.path.join(_helpers_path, 'security.py'))
_security_module = importlib.util.module_from_spec(_security_spec)
_security_spec.loader.exec_module(_security_module)

csrf_protect = _security_module.csrf_protect

logger = logging.getLogger(__name__)


def register_api_keys_routes(app: Flask):
    """Register API keys management routes."""
'''

# Extract the route definitions (everything after the imports)
# Find where imports end
import_end = content.find('@app.route')
if import_end == -1:
    print("Could not find @app.route")
    exit(1)

# Get all the route code
routes_code = content[import_end:]

# Indent all lines in routes_code by 4 spaces
indented_routes = []
for line in routes_code.split('\n'):
    if line.strip():  # Not empty
        indented_routes.append('    ' + line)
    else:
        indented_routes.append('')  # Keep empty lines

# Combine
new_content = new_header + '\n' + '\n'.join(indented_routes)

# Write back
with open(source_file, 'w') as f:
    f.write(new_content)

print(f"Transformed {source_file}")
