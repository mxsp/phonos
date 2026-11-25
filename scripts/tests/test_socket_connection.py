#!/usr/bin/env python3
"""
Test Socket.IO connection and IoT bridge functionality
"""

import requests
import json
import time
import socket

def test_server_availability():
    """Test if the server is running"""
    try:
        # Try basic HTTP connection
        response = requests.get('http://localhost:5000', timeout=5)
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        return False
    except requests.exceptions.Timeout:
        return False
    except Exception as e:
        print(f"Unexpected error: {e}")
        return False

def test_iot_endpoints():
    """Test key IoT endpoints"""
    base_url = 'http://localhost:5000'
    
    endpoints = [
        ('/iot', 'IoT page'),
        ('/api/iot/v2/devices', 'Devices API'),
        ('/api/iot/v2/device/data', 'Device data endpoint'),
        ('/api/iot/v2/device/commands', 'Device commands endpoint')
    ]
    
    results = {}
    
    for endpoint, name in endpoints:
        try:
            response = requests.get(f"{base_url}{endpoint}", timeout=5)
            results[name] = {
                'status_code': response.status_code,
                'success': response.status_code in [200, 401, 403],  # 200=OK, 401/403=auth required
                'response_time': response.elapsed.total_seconds() if hasattr(response, 'elapsed') else None
            }
        except requests.exceptions.ConnectionError:
            results[name] = {'success': False, 'error': 'Connection refused'}
        except requests.exceptions.Timeout:
            results[name] = {'success': False, 'error': 'Timeout'}
        except Exception as e:
            results[name] = {'success': False, 'error': str(e)}
    
    return results

def check_socket_io_resources():
    """Check if Socket.IO resources are available"""
    import urllib.request
    
    try:
        # Check Socket.IO client library availability
        socket_js_response = urllib.request.urlopen(
            'https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js', 
            timeout=5
        )
        return socket_js_response.getcode() == 200
    except Exception as e:
        print(f"Socket.IO resource check failed: {e}")
        return False

def main():
    """Main test function"""
    print("🔍 Testing IoT Bridge Socket.IO Connection")
    print("=" * 50)
    
    # Test 1: Server availability
    print("\n📡 Step 1: Testing Server Availability")
    server_available = test_server_availability()
    if server_available:
        print("✅ Server is running and accessible")
    else:
        print("❌ Server is not running or not accessible")
        print("   Please start the Phonos server:")
        print("   python phonos_app/app.py")
        return 1
    
    # Test 2: IoT endpoints
    print("\n🔌 Step 2: Testing IoT Endpoints")
    endpoint_results = test_iot_endpoints()
    
    for name, result in endpoint_results.items():
        status = "✅" if result['success'] else "❌"
        error_info = f" - {result['error']}" if 'error' in result else ""
        response_time = f" ({result['response_time']:.3f}s)" if result.get('response_time') else ""
        print(f"  {status} {name}: HTTP {result['status_code']}{response_time}{error_info}")
    
    # Test 3: Socket.IO resources
    print("\n🔌 Step 3: Testing Socket.IO Resources")
    socket_available = check_socket_io_resources()
    if socket_available:
        print("✅ Socket.IO client library is available")
    else:
        print("❌ Socket.IO client library not accessible")
    
    # Test 4: Summary and recommendations
    print("\n📊 Step 4: Summary & Recommendations")
    
    endpoint_success_rate = sum(1 for r in endpoint_results.values() if r['success']) / len(endpoint_results)
    
    print(f"Server Available: {'✅' if server_available else '❌'}")
    print(f"Endpoint Success Rate: {endpoint_success_rate:.1%}")
    print(f"Socket.IO Resources: {'✅' if socket_available else '❌'}")
    
    # Recommendations
    print("\n💡 Recommendations:")
    
    if not server_available:
        print("   • Start the Phonos server: python phonos_app/app.py")
        print("   • Check if port 5000 is available")
        print("   • Verify Flask application configuration")
    
    if endpoint_success_rate < 0.8:
        print("   • Check IoT API routes are properly registered")
        print("   • Verify database connection in production/config.py")
        print("   • Check for missing dependencies")
    
    if not socket_available:
        print("   • Internet connection may be required for CDN resources")
        print("   • Consider using local Socket.IO library")
        print("   • Update Socket.IO version in template to 4.7.5")
    
    if server_available and endpoint_success_rate >= 0.8 and socket_available:
        print("\n🎉 IoT Bridge should work correctly!")
        print("   The Socket.IO connection errors should be resolved.")
        print("   Try accessing http://localhost:5000/iot")
        return 0
    else:
        print("\n⚠️  Some issues remain. Address the problems above.")
        return 1

if __name__ == "__main__":
    exit(main())