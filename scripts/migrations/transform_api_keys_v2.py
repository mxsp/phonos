#!/usr/bin/env python3
"""Transform api_keys_management.py to use register function pattern - version 2."""

source_file = '/opt/phonos/phonos-main/phonos_app/routes/api_keys_management.py'
dest_file = '/opt/phonos/phonos_app/routes/api_keys_management.py'

# Read the original file
with open(source_file, 'r') as f:
    content = f.read()

# New structure - move imports inside register function
new_content = '''"""API key management endpoints - available for all users with credit-based usage."""

import logging
from flask import Flask, jsonify, request, session

logger = logging.getLogger(__name__)


def register_api_keys_routes(app: Flask):
    """Register API keys management routes."""

    # Import helpers here to avoid circular imports
    from ..helpers import login_required
    from ..helpers.security import csrf_protect
    from ..helpers.api_keys import (
        create_api_key,
        get_user_api_keys,
        delete_api_key,
        toggle_api_key,
        create_worker_api_key,
        get_user_worker_keys
    )

'''

# Extract the route definitions (everything after the original imports)
# Find where the first @app.route starts
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
final_content = new_content + '\n'.join(indented_routes)

# Write to destination
with open(dest_file, 'w') as f:
    f.write(final_content)

print(f"Transformed {dest_file}")
