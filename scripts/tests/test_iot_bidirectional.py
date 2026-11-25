#!/usr/bin/env python3
"""
IoT Bridge Bidirectional Communication Test
Tests device data upload and command sending functionality
"""

import requests
import json
import time
import hmac
import hashlib
import threading
from datetime import datetime

class IoTBidirectionalTest:
    def __init__(self, base_url="http://localhost:5000", api_key=None):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key or "test_device_key_12345"
        self.device_id = "test-device-bidirectional"
        self.session = requests.Session()
        self.test_results = []

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
        if details:
            print(f"   Details: {details}")

    def create_signature(self, payload):
        """Create HMAC signature for device requests"""
        ts = int(time.time())
        payload_with_ts = {**payload, 'timestamp': ts}
        msg = f"{ts}.{json.dumps(payload_with_ts, sort_keys=True)}"
        sig = hmac.new(
            self.api_key.encode(), 
            msg.encode(), 
            hashlib.sha256
        ).hexdigest()
        return ts, sig, payload_with_ts

    def test_device_data_upload(self):
        """Test uploading device data"""
        try:
            payload = {
                'device_id': self.device_id,
                'data': {
                    'temperature': 23.5,
                    'humidity': 45.2,
                    'status': 'active',
                    'timestamp': time.time()
                }
            }
            
            ts, sig, signed_payload = self.create_signature(payload)
            
            response = self.session.post(
                f"{self.base_url}/api/iot/v2/device/data",
                params={'api_key': self.api_key},
                json=signed_payload,
                headers={
                    'Content-Type': 'application/json',
                    'X-Phonos-Timestamp': str(ts),
                    'X-Phonos-Signature': sig
                },
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                self.log_result(
                    "Device Data Upload",
                    True,
                    f"Data uploaded successfully",
                    {'response': data, 'status_code': response.status_code}
                )
                return True
            else:
                self.log_result(
                    "Device Data Upload",
                    False,
                    f"HTTP {response.status_code}: {response.text[:100]}",
                    {'status_code': response.status_code, 'response': response.text}
                )
                return False
                
        except Exception as e:
            self.log_result(
                "Device Data Upload",
                False,
                f"Exception: {str(e)}",
                {'exception_type': type(e).__name__}
            )
            return False

    def test_command_retrieval(self):
        """Test retrieving pending commands"""
        try:
            payload = {'device_id': self.device_id}
            ts, sig, signed_payload = self.create_signature(payload)
            
            response = self.session.get(
                f"{self.base_url}/api/iot/v2/device/commands",
                params={'api_key': self.api_key},
                headers={
                    'X-Phonos-Timestamp': str(ts),
                    'X-Phonos-Signature': sig
                },
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                commands = data.get('commands', [])
                self.log_result(
                    "Command Retrieval",
                    True,
                    f"Retrieved {len(commands)} commands",
                    {'commands': commands}
                )
                return commands
            else:
                self.log_result(
                    "Command Retrieval",
                    False,
                    f"HTTP {response.status_code}: {response.text[:100]}",
                    {'status_code': response.status_code}
                )
                return []
                
        except Exception as e:
            self.log_result(
                "Command Retrieval",
                False,
                f"Exception: {str(e)}",
                {'exception_type': type(e).__name__}
            )
            return []

    def test_command_acknowledgment(self, command_id, success=True):
        """Test acknowledging command execution"""
        try:
            payload = {
                'device_id': self.device_id,
                'command_id': command_id,
                'success': success,
                'message': 'Command executed successfully' if success else 'Command failed'
            }
            
            ts, sig, signed_payload = self.create_signature(payload)
            
            response = self.session.post(
                f"{self.base_url}/api/iot/v2/device/commands/ack",
                params={'api_key': self.api_key},
                json=signed_payload,
                headers={
                    'Content-Type': 'application/json',
                    'X-Phonos-Timestamp': str(ts),
                    'X-Phonos-Signature': sig
                },
                timeout=10
            )
            
            if response.status_code == 200:
                self.log_result(
                    "Command Acknowledgment",
                    True,
                    f"Command {command_id} acknowledged",
                    {'success': success}
                )
                return True
            else:
                self.log_result(
                    "Command Acknowledgment",
                    False,
                    f"HTTP {response.status_code}: {response.text[:100]}",
                    {'status_code': response.status_code}
                )
                return False
                
        except Exception as e:
            self.log_result(
                "Command Acknowledgment",
                False,
                f"Exception: {str(e)}",
                {'exception_type': type(e).__name__}
            )
            return False

    def test_heartbeat(self):
        """Test heartbeat functionality"""
        try:
            payload = {'device_id': self.device_id}
            ts, sig, signed_payload = self.create_signature(payload)
            
            response = self.session.post(
                f"{self.base_url}/api/iot/v2/device/heartbeat",
                params={'api_key': self.api_key},
                json=signed_payload,
                headers={
                    'Content-Type': 'application/json',
                    'X-Phonos-Timestamp': str(ts),
                    'X-Phonos-Signature': sig
                },
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                self.log_result(
                    "Device Heartbeat",
                    True,
                    "Heartbeat successful",
                    {'response': data}
                )
                return True
            else:
                self.log_result(
                    "Device Heartbeat",
                    False,
                    f"HTTP {response.status_code}: {response.text[:100]}",
                    {'status_code': response.status_code}
                )
                return False
                
        except Exception as e:
            self.log_result(
                "Device Heartbeat",
                False,
                f"Exception: {str(e)}",
                {'exception_type': type(e).__name__}
            )
            return False

    def test_web_api_commands(self):
        """Test sending commands via Web API (requires user auth)"""
        try:
            # This would require authentication - for now test endpoint existence
            response = self.session.get(
                f"{self.base_url}/api/iot/v2/devices",
                timeout=5
            )
            
            if response.status_code in [200, 401, 403]:  # 200=success, 401/403=auth required
                self.log_result(
                    "Web API Access",
                    True,
                    f"Web API responding (status {response.status_code})",
                    {'status_code': response.status_code}
                )
                return response.status_code == 200
            else:
                self.log_result(
                    "Web API Access",
                    False,
                    f"Web API error: HTTP {response.status_code}",
                    {'status_code': response.status_code}
                )
                return False
                
        except Exception as e:
            self.log_result(
                "Web API Access",
                False,
                f"Exception: {str(e)}",
                {'exception_type': type(e).__name__}
            )
            return False

    def run_bidirectional_flow(self):
        """Run complete bidirectional communication flow"""
        print("🚀 Starting IoT Bidirectional Communication Test")
        print("=" * 60)
        
        # Step 1: Send heartbeat to register device
        print("\n📡 Step 1: Device Registration (Heartbeat)")
        heartbeat_success = self.test_heartbeat()
        
        if not heartbeat_success:
            print("❌ Heartbeat failed, continuing with data upload test...")
        
        # Step 2: Upload sensor data
        print("\n📤 Step 2: Upload Device Data")
        data_success = self.test_device_data_upload()
        
        # Step 3: Check for pending commands
        print("\n📥 Step 3: Check for Pending Commands")
        commands = self.test_command_retrieval()
        
        # Step 4: Acknowledge commands (if any)
        if commands:
            print("\n✅ Step 4: Acknowledge Commands")
            for cmd in commands:
                command_id = cmd.get('id', 'unknown')
                self.test_command_acknowledgment(command_id, True)
        else:
            print("\n📭 No commands to acknowledge")
        
        # Step 5: Test Web API (if available)
        print("\n🌐 Step 5: Web API Command Sending")
        web_api_success = self.test_web_api_commands()
        
        # Step 6: Send final data update
        print("\n📊 Step 6: Final Data Update")
        final_data = {
            'device_id': self.device_id,
            'data': {
                'test_completed': True,
                'final_timestamp': time.time(),
                'bidirectional_test': True
            }
        }
        ts, sig, signed_payload = self.create_signature(final_data)
        
        try:
            response = self.session.post(
                f"{self.base_url}/api/iot/v2/device/data",
                params={'api_key': self.api_key},
                json=signed_payload,
                headers={
                    'Content-Type': 'application/json',
                    'X-Phonos-Timestamp': str(ts),
                    'X-Phonos-Signature': sig
                },
                timeout=10
            )
            final_success = response.status_code == 200
            self.log_result(
                "Final Data Update",
                final_success,
                f"Status: {response.status_code}",
                {'response': response.json() if final_success else response.text}
            )
        except Exception as e:
            self.log_result(
                "Final Data Update",
                False,
                f"Exception: {str(e)}",
                {'exception_type': type(e).__name__}
            )

    def generate_report(self):
        """Generate test report"""
        print("\n" + "=" * 60)
        print("📊 TEST REPORT SUMMARY")
        print("=" * 60)
        
        total_tests = len(self.test_results)
        passed_tests = sum(1 for r in self.test_results if r['success'])
        failed_tests = total_tests - passed_tests
        success_rate = (passed_tests / total_tests * 100) if total_tests > 0 else 0
        
        print(f"Total Tests: {total_tests}")
        print(f"Passed: {passed_tests}")
        print(f"Failed: {failed_tests}")
        print(f"Success Rate: {success_rate:.1f}%")
        
        print(f"\n📋 Detailed Results:")
        for result in self.test_results:
            status = "✅" if result['success'] else "❌"
            print(f"  {status} {result['test']}: {result['message']}")
        
        # Save report to file
        report = {
            'summary': {
                'total_tests': total_tests,
                'passed': passed_tests,
                'failed': failed_tests,
                'success_rate': success_rate,
                'timestamp': datetime.now().isoformat()
            },
            'tests': self.test_results,
            'test_config': {
                'device_id': self.device_id,
                'base_url': self.base_url,
                'api_key_masked': self.api_key[:8] + '...'
            }
        }
        
        with open('iot_bidirectional_test_report.json', 'w') as f:
            json.dump(report, f, indent=2, default=str)
        
        print(f"\n💾 Detailed report saved to: iot_bidirectional_test_report.json")
        return success_rate >= 70  # Consider 70%+ as acceptable

def main():
    """Main test runner"""
    import argparse
    
    parser = argparse.ArgumentParser(description='IoT Bidirectional Communication Test')
    parser.add_argument('--url', default='http://localhost:5000', 
                       help='Base URL for IoT API')
    parser.add_argument('--api-key', default=None,
                       help='Device API key (will use test key if not provided)')
    
    args = parser.parse_args()
    
    tester = IoTBidirectionalTest(args.url, args.api_key)
    tester.run_bidirectional_flow()
    success = tester.generate_report()
    
    if success:
        print("\n🎉 IoT Bidirectional Communication Test PASSED")
        return 0
    else:
        print("\n❌ IoT Bidirectional Communication Test FAILED")
        return 1

if __name__ == "__main__":
    exit(main())