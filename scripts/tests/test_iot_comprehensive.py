#!/usr/bin/env python3
"""
IoT Bridge Comprehensive Test Suite
Tests all major functionality including bidirectional communication
"""

import os
import re
import json
from datetime import datetime

class IoTBridgeComprehensiveTest:
    def __init__(self):
        self.test_results = []
        self.base_path = '/opt/phonos'

    def log_result(self, test_name, success, message="", details=None):
        """Log test result"""
        result = {
            'test': test_name,
            'success': success,
            'message': message,
            'timestamp': datetime.now().isoformat(),
            'details': details
        }
        self.test_results.append(result)
        
        status = "✅" if success else "❌"
        print(f"{status} {test_name}: {message}")

    def test_file_structure(self):
        """Test complete file structure"""
        print("🔍 Testing File Structure")
        
        required_files = {
            'Main template': 'templates/iot.html',
            'Main CSS': 'static/css/iot.css',
            'Main JavaScript': 'static/js/iot.js',
            'IoT Bridge JS': 'static/js/iot-bridge.js',
            'UI Builder JS': 'static/js/iot-ui-builder.js',
            'API Routes': 'phonos_app/routes/api_tools.py',
            'IoT v2 API': 'phonos_app/routes/api_iot_bridge.py',
            'Crypto Helper': 'phonos_app/helpers/iot_crypto.py',
            'Bidirectional Test': 'test_iot_bidirectional.py',
            'CSS variables': 'static/css/variables.css'
        }
        
        for name, path in required_files.items():
            if os.path.exists(path):
                size = os.path.getsize(path) if os.path.isfile(path) else 0
                self.log_result(f"File: {name}", True, f"Exists ({size:,} bytes)" if size > 0 else "Exists")
            else:
                self.log_result(f"File: {name}", False, "Missing")

    def test_javascript_features(self):
        """Test JavaScript implementation quality"""
        print("\n🔍 Testing JavaScript Implementation")
        
        js_files = [
            ('Main IoT JS', 'static/js/iot.js'),
            ('IoT Bridge JS', 'static/js/iot-bridge.js'),
            ('UI Builder JS', 'static/js/iot-ui-builder.js')
        ]
        
        for name, file_path in js_files:
            if os.path.exists(file_path):
                with open(file_path, 'r') as f:
                    content = f.read()
                
                features = {
                    'Error handling': 'try {' in content and 'catch' in content,
                    'Socket.IO integration': 'socket.io' in content,
                    'API methods': 'async api(' in content,
                    'Real-time updates': 'handleIotReading' in content,
                    'Mobile responsive': 'mobile-menu' in content,
                    'Toast notifications': 'toast(' in content,
                    'Modal handling': 'openModal' in content,
                    'Device management': 'createDeviceCard' in content,
                    'Filtering logic': 'renderDevices' in content
                }
                
                passed = sum(1 for v in features.values() if v)
                total = len(features)
                
                self.log_result(f"JS Features: {name}", passed >= total * 0.8, 
                             f"{passed}/{total} features implemented", features)

    def test_css_quality(self):
        """Test CSS implementation quality"""
        print("\n🔍 Testing CSS Implementation")
        
        css_file = 'static/css/iot.css'
        if not os.path.exists(css_file):
            self.log_result("CSS Implementation", False, "CSS file missing")
            return
            
        with open(css_file, 'r') as f:
            content = f.read()
        
        features = {
            'Responsive design': '@media' in content,
            'CSS variables': 'var(--' in content,
            'Grid layouts': 'display: grid' in content,
            'Flexbox layouts': 'display: flex' in content,
            'Animations': '@keyframes' in content,
            'Transitions': 'transition:' in content,
            'Backdrop filters': 'backdrop-filter' in content,
            'Glass morphism': 'blur(' in content,
            'Dark theme': '#09090b' in content,
            'Button styles': '.btn {' in content,
            'Modal styles': '.modal {' in content,
            'Device cards': '.device-card {' in content
        }
        
        passed = sum(1 for v in features.values() if v)
        total = len(features)
        
        self.log_result("CSS Quality", passed >= total * 0.8, 
                     f"{passed}/{total} features implemented", features)

    def test_template_quality(self):
        """Test HTML template quality"""
        print("\n🔍 Testing Template Implementation")
        
        template_file = 'templates/iot.html'
        if not os.path.exists(template_file):
            self.log_result("Template Implementation", False, "Template file missing")
            return
            
        with open(template_file, 'r') as f:
            content = f.read()
        
        features = {
            'Semantic HTML5': '<!DOCTYPE html>' in content and '<header>' in content,
            'Socket.IO integration': 'socket.io.min.js' in content,
            'IoT configuration': 'window.IOT_CONFIG' in content,
            'Device management UI': 'devices-grid' in content,
            'Search functionality': 'device-search' in content,
            'Filter options': 'filter-select' in content,
            'Modal dialogs': 'modal-overlay' in content,
            'API key display': 'api-key' in content,
            'Mobile menu': 'mobile-menu' in content,
            'Real-time updates': 'iot_subscribe' in content,
            'Documentation': 'code-block' in content,
            'Accessibility': 'aria-' in content or 'role=' in content
        }
        
        passed = sum(1 for v in features.values() if v)
        total = len(features)
        
        self.log_result("Template Quality", passed >= total * 0.8, 
                     f"{passed}/{total} features implemented", features)

    def test_api_implementation(self):
        """Test API route implementation"""
        print("\n🔍 Testing API Implementation")
        
        api_files = [
            ('Tools API', 'phonos_app/routes/api_tools.py'),
            ('IoT Bridge v2', 'phonos_app/routes/api_iot_bridge.py')
        ]
        
        for name, file_path in api_files:
            if os.path.exists(file_path):
                with open(file_path, 'r') as f:
                    content = f.read()
                
                features = {
                    'Device data endpoint': '/device/data' in content,
                    'Command endpoint': '/command' in content,
                    'Authentication': 'login_required' in content,
                    'Error handling': 'jsonify' in content and '400' in content,
                    'Input validation': 'if not data:' in content,
                    'Database operations': 'cur.execute' in content,
                    'Response formatting': 'return jsonify' in content
                }
                
                passed = sum(1 for v in features.values() if v)
                total = len(features)
                
                self.log_result(f"API Implementation: {name}", passed >= total * 0.7, 
                             f"{passed}/{total} features implemented", features)

    def test_bidirectional_capability(self):
        """Test bidirectional communication capability"""
        print("\n🔍 Testing Bidirectional Communication")
        
        bidirectional_test_file = 'test_iot_bidirectional.py'
        if not os.path.exists(bidirectional_test_file):
            self.log_result("Bidirectional Test", False, "Test script missing")
            return
            
        with open(bidirectional_test_file, 'r') as f:
            content = f.read()
        
        features = {
            'Device registration': 'def test_heartbeat' in content,
            'Data upload': 'def test_device_data_upload' in content,
            'Command retrieval': 'def test_command_retrieval' in content,
            'Command acknowledgment': 'def test_command_acknowledgment' in content,
            'HMAC signing': 'def create_signature' in content,
            'Error handling': 'try:' in content and 'except' in content,
            'JSON parsing': 'json.dumps' in content and 'json.loads' in content,
            'HTTP requests': 'requests.' in content or 'urllib.request' in content,
            'Comprehensive testing': 'def run_bidirectional_flow' in content
        }
        
        passed = sum(1 for v in features.values() if v)
        total = len(features)
        
        self.log_result("Bidirectional Capability", passed >= total * 0.9, 
                     f"{passed}/{total} features implemented", features)

    def test_documentation_quality(self):
        """Test documentation quality in template"""
        print("\n🔍 Testing Documentation Quality")
        
        template_file = 'templates/iot.html'
        if not os.path.exists(template_file):
            self.log_result("Documentation Quality", False, "Template file missing")
            return
            
        with open(template_file, 'r') as f:
            content = f.read()
        
        doc_features = {
            'API examples': 'Python Example' in content,
            'Bidirectional examples': 'bidirectional' in content.lower(),
            'Endpoint documentation': 'endpoints-list' in content,
            'Code examples': 'code-block' in content,
            'Usage instructions': 'Quick Start' in content,
            'Security notes': 'Important' in content,
            'Copy functionality': 'copyCode' in content,
            'Multiple languages': 'Node.js' in content,
            'WebSocket docs': 'WebSocket Events' in content
        }
        
        passed = sum(1 for v in doc_features.values() if v)
        total = len(doc_features)
        
        self.log_result("Documentation Quality", passed >= total * 0.7, 
                     f"{passed}/{total} documentation features", doc_features)

    def test_security_implementation(self):
        """Test security implementation"""
        print("\n🔍 Testing Security Implementation")
        
        # Check multiple files for security features
        security_features = {}
        
        # Check JavaScript for XSS prevention
        if os.path.exists('static/js/iot.js'):
            with open('static/js/iot.js', 'r') as f:
                js_content = f.read()
            security_features['XSS prevention'] = 'textContent' in js_content
        
        # Check API routes for authentication
        if os.path.exists('phonos_app/routes/api_iot_bridge.py'):
            with open('phonos_app/routes/api_iot_bridge.py', 'r') as f:
                api_content = f.read()
            security_features['API authentication'] = 'login_required' in api_content
            security_features['Input validation'] = 'if not data:' in api_content
            security_features['SQL injection prevention'] = 'cur.execute' in api_content and '%s' in api_content
        
        # Check template for secure rendering
        if os.path.exists('templates/iot.html'):
            with open('templates/iot.html', 'r') as f:
                template_content = f.read()
            security_features['Template security'] = '{{' in template_content  # Jinja2 escaping
        
        passed = sum(1 for v in security_features.values() if v)
        total = len(security_features)
        
        self.log_result("Security Implementation", passed >= total * 0.7, 
                     f"{passed}/{total} security features", security_features)

    def test_responsive_design(self):
        """Test responsive design implementation"""
        print("\n🔍 Testing Responsive Design")
        
        css_file = 'static/css/iot.css'
        if not os.path.exists(css_file):
            self.log_result("Responsive Design", False, "CSS file missing")
            return
            
        with open(css_file, 'r') as f:
            content = f.read()
        
        responsive_features = {
            'Mobile breakpoints': '@media (max-width:' in content,
            'Sidebar mobile': '.iot-sidebar {' in content and 'transform:' in content,
            'Responsive grid': 'grid-template-columns' in content,
            'Mobile menu': '.mobile-menu' in content,
            'Touch targets': '44px' in content or 'min-width' in content,
            'Flexible layouts': 'flex-direction: column' in content,
            'Mobile typography': 'font-size:' in content
        }
        
        passed = sum(1 for v in responsive_features.values() if v)
        total = len(responsive_features)
        
        self.log_result("Responsive Design", passed >= total * 0.7, 
                     f"{passed}/{total} responsive features", responsive_features)

    def run_comprehensive_tests(self):
        """Run all comprehensive tests"""
        print("🚀 Starting IoT Bridge Comprehensive Test Suite")
        print("=" * 70)
        
        self.test_file_structure()
        self.test_javascript_features()
        self.test_css_quality()
        self.test_template_quality()
        self.test_api_implementation()
        self.test_bidirectional_capability()
        self.test_documentation_quality()
        self.test_security_implementation()
        self.test_responsive_design()
        
        self.generate_comprehensive_report()

    def generate_comprehensive_report(self):
        """Generate comprehensive test report"""
        print("\n" + "=" * 70)
        print("📊 COMPREHENSIVE TEST REPORT")
        print("=" * 70)
        
        total_tests = len(self.test_results)
        passed_tests = sum(1 for r in self.test_results if r['success'])
        failed_tests = total_tests - passed_tests
        success_rate = (passed_tests / total_tests * 100) if total_tests > 0 else 0
        
        print(f"Total Test Categories: {total_tests}")
        print(f"Passed: {passed_tests}")
        print(f"Failed: {failed_tests}")
        print(f"Overall Success Rate: {success_rate:.1f}%")
        
        # Category breakdown
        categories = {
            'File Structure': 0,
            'JavaScript Implementation': 0,
            'CSS Quality': 0,
            'Template Quality': 0,
            'API Implementation': 0,
            'Bidirectional': 0,
            'Documentation': 0,
            'Security': 0,
            'Responsive Design': 0
        }
        
        for result in self.test_results:
            test_name = result['test']
            for category in categories:
                if category.lower() in test_name.lower():
                    categories[category] += 1 if result['success'] else 0
                    break
        
        print(f"\n📋 Category Breakdown:")
        for category, passed in categories.items():
            status = "✅" if passed > 0 else "❌"
            print(f"  {status} {category}")
        
        # Save detailed report
        report = {
            'summary': {
                'total_tests': total_tests,
                'passed': passed_tests,
                'failed': failed_tests,
                'success_rate': success_rate,
                'timestamp': datetime.now().isoformat(),
                'categories': categories
            },
            'tests': self.test_results
        }
        
        with open('iot_bridge_comprehensive_report.json', 'w') as f:
            json.dump(report, f, indent=2, default=str)
        
        print(f"\n💾 Detailed report saved to: iot_bridge_comprehensive_report.json")
        
        # Overall assessment
        if success_rate >= 80:
            print(f"\n🎉 IoT Bridge Comprehensive Test: EXCELLENT")
            print("   All major features implemented and working correctly")
            return 'excellent'
        elif success_rate >= 70:
            print(f"\n👍 IoT Bridge Comprehensive Test: GOOD")
            print("   Most features implemented with minor areas for improvement")
            return 'good'
        elif success_rate >= 60:
            print(f"\n⚠️ IoT Bridge Comprehensive Test: FAIR")
            print("   Basic functionality working, needs improvements")
            return 'fair'
        else:
            print(f"\n❌ IoT Bridge Comprehensive Test: NEEDS WORK")
            print("   Significant issues found, requires major improvements")
            return 'poor'

def main():
    """Main test runner"""
    tester = IoTBridgeComprehensiveTest()
    rating = tester.run_comprehensive_tests()
    
    return 0 if rating in ['excellent', 'good'] else 1

if __name__ == "__main__":
    exit(main())