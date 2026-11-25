#!/usr/bin/env python3
"""
Test the specific IoT bridge improvements made.
"""

import re
import os

def test_iot_js_improvements():
    """Test specific improvements in iot.js."""
    print("🔍 Testing IoT.js improvements...")
    
    with open('static/js/iot.js', 'r') as f:
        content = f.read()
    
    improvements = {
        'Better error handling': 'error.message || \'Request failed\'',
        'Loading states': 'submitBtn.textContent = \'Adding...\'',
        'Device API key modal': 'showDeviceApiKey',
        'Socket reconnection': 'reconnectionAttempts: 5',
        'Filter improvements': 'updateFilterSummary',
        'Type filter': 'device-type-filter',
        'Clear filters': 'clearFilters',
        'Mobile sidebar': 'document.body.style.overflow',
        'Command validation': '!this.currentDevice.is_online'
    }
    
    for feature, pattern in improvements.items():
        if pattern in content:
            print(f"  ✅ {feature}")
        else:
            print(f"  ❌ {feature} - Pattern: {pattern}")

def test_template_improvements():
    """Test specific improvements in iot.html."""
    print("\n🔍 Testing IoT template improvements...")
    
    with open('templates/iot.html', 'r') as f:
        content = f.read()
    
    improvements = {
        'Type filter dropdown': 'device-type-filter',
        'Filter summary': 'filter-summary',
        'Template options': 'template-btn',
        'Device cards grid': 'devices-grid',
        'Mobile menu button': 'mobile-menu'
    }
    
    for feature, element in improvements.items():
        if element in content:
            print(f"  ✅ {feature}")
        else:
            print(f"  ❌ {feature}")

def test_css_improvements():
    """Test CSS improvements."""
    print("\n🔍 Testing IoT CSS improvements...")
    
    with open('static/css/iot.css', 'r') as f:
        content = f.read()
    
    improvements = {
        'Mobile sidebar': '.iot-sidebar {',
        'Device cards': '.device-card {',
        'Toast notifications': '.toast {',
        'Modal styles': '.modal-overlay {',
        'Builder styles': '.builder-workspace {',
        'Responsive grid': 'grid-template-columns: repeat',
        'Animation keyframes': '@keyframes'
    }
    
    for feature, pattern in improvements.items():
        if pattern in content:
            print(f"  ✅ {feature}")
        else:
            print(f"  ❌ {feature}")

def test_api_improvements():
    """Test API route improvements."""
    print("\n🔍 Testing API improvements...")
    
    # Check api_tools.py for IoT route
    with open('phonos_app/routes/api_tools.py', 'r') as f:
        tools_content = f.read()
    
    # Check api_iot_bridge.py exists
    bridge_exists = os.path.exists('phonos_app/routes/api_iot_bridge.py')
    
    improvements = {
        'IoT page route': '@app.route(\'/iot\')',
        'IoT builder route': '@app.route(\'/iot/builder/',
        'API key generation': 'secrets.token_hex',
        'V2 API endpoints': 'api_iot_bridge.py'
    }
    
    if '@app.route(\'/iot\')' in tools_content:
        print("  ✅ IoT page route")
    else:
        print("  ❌ IoT page route")
    
    if '@app.route(\'/iot/builder/' in tools_content:
        print("  ✅ IoT builder route")
    else:
        print("  ❌ IoT builder route")
    
    if 'secrets.token_hex' in tools_content:
        print("  ✅ API key generation")
    else:
        print("  ❌ API key generation")
        
    print("  ✅" if bridge_exists else "  ❌", "V2 API endpoints file")

def test_file_sizes():
    """Test file sizes to ensure reasonable loading times."""
    print("\n🔍 Testing file sizes...")
    
    files_to_check = {
        'static/js/iot.js': 100000,  # ~100KB max
        'static/css/iot.css': 60000,  # ~60KB max
        'templates/iot.html': 50000    # ~50KB max
    }
    
    for file_path, max_size in files_to_check.items():
        if os.path.exists(file_path):
            size = os.path.getsize(file_path)
            status = "✅" if size < max_size else "⚠️"
            print(f"  {status} {file_path}: {size:,} bytes (max: {max_size:,})")
        else:
            print(f"  ❌ {file_path} not found")

def main():
    """Run all improvement tests."""
    print("🚀 Testing IoT Bridge Improvements")
    print("=" * 50)
    
    test_file_sizes()
    test_template_improvements()
    test_iot_js_improvements()
    test_css_improvements()
    test_api_improvements()
    
    print("\n" + "=" * 50)
    print("✅ IoT Bridge improvements testing complete!")

if __name__ == "__main__":
    main()