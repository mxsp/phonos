#!/usr/bin/env python3
"""
Simple test script to verify IoT bridge functionality.
This tests the core components without needing to run the full app.
"""

import sys
import os

# Add the project root to Python path
sys.path.insert(0, '/opt/phonos')

def test_iot_javascript():
    """Test JavaScript syntax for IoT files."""
    import subprocess
    import os
    
    js_files = [
        'static/js/iot.js',
        'static/js/iot-bridge.js'
    ]
    
    print("🔍 Testing JavaScript files...")
    for js_file in js_files:
        if os.path.exists(js_file):
            try:
                result = subprocess.run(['node', '--check', js_file], 
                                      capture_output=True, text=True)
                if result.returncode == 0:
                    print(f"✅ {js_file} - Syntax OK")
                else:
                    print(f"❌ {js_file} - Syntax Error:")
                    print(result.stderr)
            except FileNotFoundError:
                print(f"⚠️ Node.js not found, skipping {js_file}")
        else:
            print(f"❌ {js_file} not found")

def test_iot_templates():
    """Test that IoT templates exist and have required elements."""
    template_files = [
        'templates/iot.html'
    ]
    
    print("\n🔍 Testing template files...")
    for template in template_files:
        if os.path.exists(template):
            with open(template, 'r') as f:
                content = f.read()
                # Check for key elements
                checks = {
                    'socket.io': 'socket.io.min.js' in content,
                    'IoT object': 'window.IOT_CONFIG' in content,
                    'device grid': 'devices-grid' in content,
                    'add device modal': 'modal-add' in content,
                    'API key display': 'api-key' in content
                }
                
                print(f"✅ {template} exists")
                for check, passed in checks.items():
                    status = "✅" if passed else "❌"
                    print(f"  {status} {check}")
        else:
            print(f"❌ {template} not found")

def test_static_files():
    """Test that required static files exist."""
    static_files = [
        'static/css/iot.css',
        'static/js/iot.js',
        'static/js/iot-ui-builder.js'
    ]
    
    print("\n🔍 Testing static files...")
    for static_file in static_files:
        if os.path.exists(static_file):
            size = os.path.getsize(static_file)
            print(f"✅ {static_file} ({size} bytes)")
        else:
            print(f"❌ {static_file} not found")

import py_compile

def test_api_routes():
    """Test that API route files exist and can be imported."""
    route_files = [
        'phonos_app/routes/api_tools.py',
        'phonos_app/routes/api_iot_bridge.py'
    ]
    
    print("\n🔍 Testing API route files...")
    for route_file in route_files:
        if os.path.exists(route_file):
            try:
                # Test Python compilation
                py_compile.compile(route_file, doraise=True)
                print(f"✅ {route_file} - Compiles OK")
            except py_compile.PyCompileError as e:
                print(f"❌ {route_file} - Compile Error:")
                print(f"   {e}")
        else:
            print(f"❌ {route_file} not found")

def main():
    """Run all tests."""
    print("🚀 Starting IoT Bridge Test Suite")
    print("=" * 50)
    
    test_static_files()
    test_iot_templates()
    test_iot_javascript()
    test_api_routes()
    
    print("\n" + "=" * 50)
    print("✅ IoT Bridge testing complete!")

if __name__ == "__main__":
    main()