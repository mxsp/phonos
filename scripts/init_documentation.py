#!/usr/bin/env python3
"""
Initialize documentation pages in the database.
Run this script to populate the documentation_pages table with default content.
"""

import sys
import os

# Add parent directory to path to import modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from phonos_app.helpers.documentation import initialize_documentation

def main():
    print("Initializing documentation...")
    try:
        initialize_documentation()
        print("✓ Documentation initialized successfully!")
        print("\nYou can now access the documentation at:")
        print("  - Documentation index: /docs")
        print("  - Help settings: Settings > Help tab")
        print("  - Main website link: https://fyreflysystems.de")
    except Exception as e:
        print(f"✗ Error initializing documentation: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == '__main__':
    main()
