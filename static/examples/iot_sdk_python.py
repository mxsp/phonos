"""
Phonos IoT Bridge - Python SDK

A simple Python client for connecting devices to the Phonos IoT Bridge.
Works on Raspberry Pi, Linux, macOS, Windows, and MicroPython (ESP32/ESP8266).

Installation:
    pip install requests

Usage:
    from phonos_iot import PhonosDevice

    device = PhonosDevice(
        api_key="dk_your_device_api_key",
        server_url="https://your-phonos-server.com"
    )

    # Send sensor data
    device.send_data({
        "temperature": 23.5,
        "humidity": 45.2
    })

    # Check for commands
    commands = device.get_commands()
    for cmd in commands:
        if cmd['target'] == 'relay':
            set_relay(cmd['payload']['value'])
        device.ack_command(cmd['command_id'], success=True)
"""

import json
import time
import hashlib
import hmac
import base64
import threading
from typing import Dict, Any, Optional, List, Callable

try:
    import requests
except ImportError:
    requests = None

try:
    # MicroPython support
    import urequests as requests
    import ubinascii
    import uhashlib
except ImportError:
    pass


class PhonosDevice:
    """Phonos IoT Bridge device client."""

    def __init__(
        self,
        api_key: str,
        server_url: str = "https://phonos.io",
        encryption: bool = True,
        auto_heartbeat: bool = True,
        heartbeat_interval: int = 60
    ):
        """
        Initialize the Phonos IoT device client.

        Args:
            api_key: Device API key (starts with 'dk_')
            server_url: Phonos server URL
            encryption: Enable encryption for data (recommended)
            auto_heartbeat: Automatically send heartbeats
            heartbeat_interval: Heartbeat interval in seconds
        """
        self.api_key = api_key
        self.server_url = server_url.rstrip('/')
        self.encryption = encryption
        self.base_url = f"{self.server_url}/api/iot/v2"

        self._command_handlers: Dict[str, Callable] = {}
        self._heartbeat_thread = None
        self._running = False

        if auto_heartbeat:
            self.start_heartbeat(heartbeat_interval)

    def _get_headers(self, payload: str = "") -> Dict[str, str]:
        """Generate authentication headers with signature."""
        timestamp = int(time.time())
        signature = self._sign(timestamp, payload)

        return {
            "X-Device-Key": self.api_key,
            "X-Timestamp": str(timestamp),
            "X-Signature": signature,
            "Content-Type": "application/json"
        }

    def _sign(self, timestamp: int, payload: str) -> str:
        """Create HMAC-SHA256 signature."""
        message = f"{timestamp}.{payload}"
        signature = hmac.new(
            self.api_key.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    def _encrypt(self, data: Dict[str, Any]) -> str:
        """Encrypt data using the API key (simple XOR + HMAC for compatibility)."""
        plaintext = json.dumps(data, separators=(',', ':')).encode('utf-8')
        key_bytes = hashlib.sha256(self.api_key.encode('utf-8')).digest()

        # XOR encryption
        key_stream = (key_bytes * ((len(plaintext) // 32) + 1))[:len(plaintext)]
        ciphertext = bytes(a ^ b for a, b in zip(plaintext, key_stream))

        # HMAC for integrity
        mac = hmac.new(key_bytes, ciphertext, hashlib.sha256).digest()

        # Pack: version(1) + mac(32) + ciphertext
        packed = b'\x00' + mac + ciphertext
        return base64.b64encode(packed).decode('ascii')

    def _decrypt(self, encrypted: str) -> Dict[str, Any]:
        """Decrypt data using the API key."""
        packed = base64.b64decode(encrypted)

        version = packed[0]
        if version != 0:
            raise ValueError(f"Unsupported encryption version: {version}")

        mac = packed[1:33]
        ciphertext = packed[33:]

        key_bytes = hashlib.sha256(self.api_key.encode('utf-8')).digest()
        expected_mac = hmac.new(key_bytes, ciphertext, hashlib.sha256).digest()

        if not hmac.compare_digest(mac, expected_mac):
            raise ValueError("HMAC verification failed")

        key_stream = (key_bytes * ((len(ciphertext) // 32) + 1))[:len(ciphertext)]
        plaintext = bytes(a ^ b for a, b in zip(ciphertext, key_stream))

        return json.loads(plaintext.decode('utf-8'))

    def send_data(
        self,
        data: Dict[str, Any],
        data_type: str = "reading",
        source: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Send data to the server.

        Args:
            data: Dictionary of sensor readings or status data
            data_type: Type of data ('reading', 'event', 'status', 'error')
            source: Optional source identifier (e.g., sensor name)

        Returns:
            Server response including any pending commands

        Example:
            device.send_data({
                "temperature": 23.5,
                "humidity": 45.2,
                "pressure": 1013.25
            })
        """
        body = {
            "type": data_type,
            "device_timestamp": int(time.time())
        }

        if source:
            body["source"] = source

        if self.encryption:
            body["encrypted"] = True
            body["data"] = self._encrypt(data)
        else:
            body["data"] = data

        payload = json.dumps(body)
        headers = self._get_headers(payload)

        response = requests.post(
            f"{self.base_url}/device/data",
            headers=headers,
            data=payload
        )

        result = response.json()

        # Handle any commands returned with the response
        if "commands" in result:
            commands = result["commands"]
            if isinstance(commands, dict) and commands.get("encrypted"):
                commands = self._decrypt(commands["data"])
                if isinstance(commands, dict) and "commands" in commands:
                    commands = commands["commands"]
            self._process_commands(commands)

        return result

    def get_commands(self) -> List[Dict[str, Any]]:
        """
        Poll for pending commands.

        Returns:
            List of pending commands

        Example:
            commands = device.get_commands()
            for cmd in commands:
                print(f"Command: {cmd['type']} -> {cmd['target']}")
        """
        headers = self._get_headers()

        response = requests.get(
            f"{self.base_url}/device/commands",
            headers=headers
        )

        result = response.json()

        if result.get("encrypted"):
            result = self._decrypt(result["data"])

        commands = result.get("commands", [])
        return commands

    def ack_command(
        self,
        command_id: str,
        success: bool = True,
        response: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Acknowledge command execution.

        Args:
            command_id: The command ID to acknowledge
            success: Whether the command executed successfully
            response: Optional response data
            error: Optional error message if failed

        Example:
            device.ack_command(cmd['command_id'], success=True, response={"status": "done"})
        """
        body = {
            "command_id": command_id,
            "success": success
        }

        if response:
            body["response"] = response
        if error:
            body["error"] = error

        payload = json.dumps(body)
        headers = self._get_headers(payload)

        response = requests.post(
            f"{self.base_url}/device/commands/ack",
            headers=headers,
            data=payload
        )

        return response.json()

    def heartbeat(self) -> Dict[str, Any]:
        """Send a heartbeat to maintain online status."""
        headers = self._get_headers()

        response = requests.post(
            f"{self.base_url}/device/heartbeat",
            headers=headers
        )

        return response.json()

    def on_command(self, target: str, handler: Callable):
        """
        Register a command handler for a specific target.

        Args:
            target: The target/input name to handle
            handler: Callback function that receives the command payload

        Example:
            @device.on_command("relay")
            def handle_relay(payload):
                set_relay(payload['value'])
                return {"status": "ok"}
        """
        self._command_handlers[target] = handler
        return handler

    def _process_commands(self, commands: List[Dict[str, Any]]):
        """Process received commands using registered handlers."""
        for cmd in commands:
            target = cmd.get("target")
            if target in self._command_handlers:
                try:
                    response = self._command_handlers[target](cmd.get("payload", {}))
                    self.ack_command(cmd["command_id"], success=True, response=response)
                except Exception as e:
                    self.ack_command(cmd["command_id"], success=False, error=str(e))

    def start_heartbeat(self, interval: int = 60):
        """Start automatic heartbeat thread."""
        self._running = True

        def heartbeat_loop():
            while self._running:
                try:
                    self.heartbeat()
                except Exception:
                    pass
                time.sleep(interval)

        self._heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def stop(self):
        """Stop the heartbeat thread."""
        self._running = False

    def run(self, poll_interval: int = 5):
        """
        Run the device loop, polling for commands.

        Args:
            poll_interval: How often to poll for commands (seconds)

        Example:
            @device.on_command("relay")
            def handle_relay(payload):
                GPIO.output(RELAY_PIN, payload['value'])

            device.run()  # Blocks and handles commands
        """
        print(f"Phonos IoT device running (poll every {poll_interval}s)...")
        self._running = True

        while self._running:
            try:
                commands = self.get_commands()
                self._process_commands(commands)
            except Exception as e:
                print(f"Error polling commands: {e}")

            time.sleep(poll_interval)


# =============================================================================
# DEVICE REGISTRATION HELPER
# =============================================================================

def register_device(
    user_api_key: str,
    device_id: str,
    device_name: str,
    server_url: str = "https://phonos.io",
    device_type: str = "generic",
    schema: Optional[Dict] = None
) -> Dict[str, Any]:
    """
    Register a new device and get its API key.

    Args:
        user_api_key: Your Phonos user API key
        device_id: Unique device identifier (alphanumeric with hyphens/underscores)
        device_name: Human-readable device name
        server_url: Phonos server URL
        device_type: Device type category
        schema: Optional I/O schema definition

    Returns:
        Registration response including device_api_key

    Example:
        result = register_device(
            user_api_key="your_user_api_key",
            device_id="lab-sensor-01",
            device_name="Lab Temperature Sensor"
        )
        print(f"Device API Key: {result['device_api_key']}")
    """
    body = {
        "user_api_key": user_api_key,
        "device_id": device_id,
        "device_name": device_name,
        "device_type": device_type
    }

    if schema:
        body["schema"] = schema

    response = requests.post(
        f"{server_url.rstrip('/')}/api/iot/v2/device/register",
        headers={"Content-Type": "application/json"},
        data=json.dumps(body)
    )

    return response.json()


# =============================================================================
# EXAMPLE USAGE
# =============================================================================

if __name__ == "__main__":
    # Example: Register a new device
    # result = register_device(
    #     user_api_key="your_user_api_key",
    #     device_id="my-sensor-01",
    #     device_name="Living Room Sensor",
    #     server_url="https://phonos.io"
    # )
    # print(f"Save this API key: {result['device_api_key']}")

    # Example: Use an existing device
    DEVICE_API_KEY = "dk_your_device_api_key_here"
    SERVER_URL = "https://phonos.io"

    device = PhonosDevice(
        api_key=DEVICE_API_KEY,
        server_url=SERVER_URL,
        encryption=True
    )

    # Register command handlers
    @device.on_command("led")
    def handle_led(payload):
        value = payload.get("value", False)
        print(f"Setting LED to: {value}")
        # GPIO.output(LED_PIN, value)
        return {"status": "ok", "led": value}

    @device.on_command("config")
    def handle_config(payload):
        print(f"Received config: {payload}")
        return {"status": "configured"}

    # Send some sample data
    device.send_data({
        "temperature": 23.5,
        "humidity": 45.0,
        "light": 512
    })

    print("Device connected! Waiting for commands...")
    # device.run()  # Uncomment to run the command loop
