#!/usr/bin/env python3
"""Fix indentation in api_keys_management.py to wrap routes in register function."""

import os

file_path = '/opt/phonos/phonos_app/routes/api_keys_management.py'

# Read the file
with open(file_path, 'r') as f:
    lines = f.readlines()

# Find where the register function starts (around line 41)
register_line = None
for i, line in enumerate(lines):
    if 'def register_api_keys_routes(' in line:
        register_line = i
        break

if register_line is None:
    print("Could not find register function")
    exit(1)

# Indent all lines after the register function by 4 spaces
# (except for lines that are already empty or only whitespace)
new_lines = lines[:register_line+2]  # Keep header and function def

for i in range(register_line+2, len(lines)):
    line = lines[i]
    # Don't indent empty lines
    if line.strip() == '':
        new_lines.append(line)
    else:
        # Add 4 spaces of indentation
        new_lines.append('    ' + line)

# Write back
with open(file_path, 'w') as f:
    f.writelines(new_lines)

print(f"Fixed indentation in {file_path}")
